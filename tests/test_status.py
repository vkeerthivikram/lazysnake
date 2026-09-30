"""Porcelain-v2 status parser tests: recorded fixtures + a live repository."""

from __future__ import annotations

from lazysnake.git.models import RepoSnapshot
from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status

FIXTURE = (
    "\0".join(
        [
            "# branch.oid abcdef1234567890",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -1",
            "1 M. N... 100644 100644 100644 h1 h2 staged-only.txt",
            "1 .M N... 100644 100644 100644 h2 h2 unstaged-only.txt",
            "1 MM N... 100644 100644 100644 h1 h2 both.txt",
            "1 A. N... 000000 100644 100644 000000 h3 new-staged.txt",
            "2 R. N... 100644 100644 100644 h4 h5 R100 renamed-new.txt",
            "renamed-old.txt",
            "u UU N... 000000 100644 100644 000000 h6 h7 h8 conflicted.txt",
            "? untracked.txt",
        ]
    )
    + "\0"
)


def test_fixture_branch_header() -> None:
    snap = parse_status(FIXTURE)
    assert snap.branch == "main"
    assert snap.oid == "abcdef1234567890"
    assert snap.upstream == "origin/main"
    assert snap.ahead == 2
    assert snap.behind == 1


def test_fixture_files() -> None:
    snap = parse_status(FIXTURE)
    by_path = {f.path: f for f in snap.files}
    assert len(snap.files) == 7

    assert by_path["staged-only.txt"].display == "M "
    assert by_path["staged-only.txt"].staged
    assert not by_path["staged-only.txt"].unstaged

    assert by_path["unstaged-only.txt"].display == " M"
    assert not by_path["unstaged-only.txt"].staged
    assert by_path["unstaged-only.txt"].unstaged

    both = by_path["both.txt"]
    assert both.display == "MM"
    assert both.staged and both.unstaged

    assert by_path["new-staged.txt"].display == "A "

    renamed = by_path["renamed-new.txt"]
    assert renamed.orig_path == "renamed-old.txt"
    assert renamed.display == "R "

    conflicted = by_path["conflicted.txt"]
    assert conflicted.unmerged
    assert conflicted.display == "UU"
    assert snap.in_conflict
    assert snap.conflict_files == [conflicted]

    assert by_path["untracked.txt"].display == "??"
    assert by_path["untracked.txt"].untracked


def test_snapshot_grouping() -> None:
    snap: RepoSnapshot = parse_status(FIXTURE)
    assert [f.path for f in snap.staged_files] == [
        "staged-only.txt",
        "both.txt",
        "new-staged.txt",
        "renamed-new.txt",
    ]
    assert {f.path for f in snap.unstaged_files} == {
        "unstaged-only.txt",
        "both.txt",
        "conflicted.txt",
        "untracked.txt",
    }


def test_initial_repo_state() -> None:
    snap = parse_status("# branch.oid (initial)\0# branch.head (initial)\0")
    assert snap.oid is None
    assert snap.branch == "(initial)"
    assert snap.files == []


async def test_live_repository(repo) -> None:
    git = Git(repo.root)
    repo.write("new-file.txt", "hi\n")
    repo.write("README.md", "changed\n")

    data = await git.run("status", "--porcelain=v2", "--branch", "-z")
    snap = parse_status(data)

    assert snap.branch == "main"
    assert snap.oid is not None and len(snap.oid) == 40
    assert snap.upstream is None  # no remote configured
    by_path = {f.path: f for f in snap.files}
    assert by_path["README.md"].display == " M"
    assert by_path["new-file.txt"].display == "??"


async def test_live_staged_and_renamed(repo) -> None:
    git = Git(repo.root)
    repo.git("mv", "README.md", "INTRO.md")

    data = await git.run("status", "--porcelain=v2", "--branch", "-z")
    snap = parse_status(data)
    entry = next(f for f in snap.files if f.path == "INTRO.md")
    assert entry.orig_path == "README.md"
    assert entry.display == "R "
    assert entry.staged
