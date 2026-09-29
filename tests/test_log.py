"""Commit-log parser tests against a live repository."""

from __future__ import annotations

from lazysnake.git.log import FORMAT, parse_log
from lazysnake.git.runner import Git


async def test_parse_live_log(repo) -> None:
    git = Git(repo.root)
    repo.write("a.txt", "a\n")
    repo.commit_all("second commit")
    repo.write("b.txt", "b\n")
    repo.commit_all("third commit")

    raw = await git.run("log", f"--pretty=format:{FORMAT}")
    commits = parse_log(raw)

    assert len(commits) == 3
    # Newest first.
    assert [c.subject for c in commits] == [
        "third commit",
        "second commit",
        "initial commit",
    ]
    head = commits[0]
    assert head.author == "Test Snake"
    assert head.short_sha == head.sha[: len(head.short_sha)]
    assert len(head.sha) == 40
    assert head.timestamp > 0
    assert "HEAD -> main" in head.refs
    assert commits[-1].refs == ""


async def test_parse_fuzzy_input() -> None:
    """Empty or truncated streams must not explode."""
    assert parse_log("") == []
    assert parse_log("\x1e\n\x1e") == []
    # A record missing trailing fields is padded, not dropped.
    commits = parse_log("abc123\x1fabc\x1fX\x1f1700000000\x1fhello\x1e")
    assert len(commits) == 1
    assert commits[0].refs == ""
    assert commits[0].subject == "hello"
