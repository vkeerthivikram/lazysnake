"""Branch and stash parser tests against a live repository."""

from __future__ import annotations

from lazysnake.git.branch import FORMAT as BRANCH_FORMAT
from lazysnake.git.branch import parse_branches
from lazysnake.git.runner import Git
from lazysnake.git.stash import FORMAT as STASH_FORMAT
from lazysnake.git.stash import parse_stash


async def test_parse_live_branches(repo) -> None:
    git = Git(repo.root)
    repo.git("switch", "-c", "feature/x")
    repo.git("switch", "main")

    data = await git.run("for-each-ref", "refs/heads", f"--format={BRANCH_FORMAT}")
    branches = parse_branches(data)
    by_name = {b.name: b for b in branches}

    assert set(by_name) == {"main", "feature/x"}
    assert by_name["main"].is_head
    assert not by_name["feature/x"].is_head
    # HEAD first, then alphabetical.
    assert branches[0].name == "main"
    assert branches[1].name == "feature/x"
    assert len(by_name["main"].sha) == 40


def test_parse_branch_fixture() -> None:
    sep = "\x1f"
    data = "\n".join(
        [
            f"main{sep}abc123{sep}*{sep}origin/main{sep}[ahead 2]",
            f"dev{sep}def456{sep} {sep}origin/dev{sep}[behind 1]",
            f"orphan{sep}789aaa{sep} {sep}origin/orphan{sep}[gone]",
        ]
    )
    branches = parse_branches(data)
    main, dev, orphan = branches
    assert main.is_head and main.upstream == "origin/main" and main.track == "ahead 2"
    assert not dev.is_head and dev.track == "behind 1"
    assert orphan.track == "gone"


async def test_parse_live_stash(repo) -> None:
    git = Git(repo.root)
    repo.write("README.md", "wip change\n")
    repo.git("stash", "push", "-m", "my work in progress")

    data = await git.run("stash", "list", f"--format={STASH_FORMAT}")
    entries = parse_stash(data)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.index == 0
    assert entry.name == "stash@{0}"
    assert len(entry.sha) == 40
    assert "my work in progress" in entry.subject
    assert entry.when  # timestamp recorded


def test_parse_stash_empty() -> None:
    assert parse_stash("") == []
