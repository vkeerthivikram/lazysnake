"""Hunk- and line-level staging against a real repository."""

from __future__ import annotations

import pytest

from lazysnake.git.diff import parse_diff
from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status
from lazysnake.staging import hunk_patch, line_patch

SEED = "\n".join(f"line {i}" for i in range(1, 16)) + "\n"


async def _make_two_hunk_repo(repo) -> Git:
    """A repo whose tracked file has two independent modified hunks."""
    repo.write("file.txt", SEED)
    repo.commit_all("seed file")
    changed = SEED.replace("line 2\n", "line 2 changed\n").replace("line 14\n", "line 14 changed\n")
    repo.write("file.txt", changed)
    return Git(repo.root)


async def _staged_raw(git: Git, path: str) -> str:
    return await git.run("diff", "--no-color", "--cached", "--", path)


async def test_stage_single_hunk(repo) -> None:
    git = await _make_two_hunk_repo(repo)
    files = parse_diff(await git.run("diff", "--no-color"))
    fd = files[0]
    assert len(fd.hunks) == 2

    await git.run("apply", "--cached", input=hunk_patch(fd, fd.hunks[0]))

    staged = parse_diff(await _staged_raw(git, "file.txt"))
    assert len(staged[0].hunks) == 1
    assert "+line 2 changed" in staged[0].hunks[0].to_patch()
    assert "line 14 changed" not in staged[0].to_patch()


async def test_stage_single_line(repo) -> None:
    git = await _make_two_hunk_repo(repo)
    files = parse_diff(await git.run("diff", "--no-color"))
    fd = files[0]
    hunk = fd.hunks[0]
    add_idx = next(i for i, pl in enumerate(hunk.lines) if pl.content == "line 2 changed")

    await git.run("apply", "--cached", input=line_patch(fd, hunk, {add_idx}))

    staged = parse_diff(await _staged_raw(git, "file.txt"))
    assert len(staged[0].hunks) == 1
    patch_text = staged[0].to_patch()
    assert "+line 2 changed" in patch_text
    assert "line 14 changed" not in patch_text
    # Single line swap: symmetric hunk header.
    assert staged[0].hunks[0].old_count == staged[0].hunks[0].new_count


async def test_unstage_via_reverse_apply(repo) -> None:
    git = await _make_two_hunk_repo(repo)
    repo.git("add", "file.txt")

    files = parse_diff(await git.run("diff", "--no-color", "--cached"))
    fd = files[0]
    await git.run("apply", "--reverse", "--cached", input=hunk_patch(fd, fd.hunks[1]))

    staged = parse_diff(await _staged_raw(git, "file.txt"))
    assert len(staged[0].hunks) == 1
    snap = parse_status(await git.run("status", "--porcelain=v2", "--branch", "-z"))
    by_path = {f.path: f for f in snap.files}
    # line 2 change still staged, line 14 change now worktree-only.
    assert by_path["file.txt"].display == "MM"


async def test_line_patch_includes_context(repo) -> None:
    git = await _make_two_hunk_repo(repo)
    files = parse_diff(await git.run("diff", "--no-color"))
    fd = files[0]
    hunk = fd.hunks[0]
    add_idx = next(i for i, pl in enumerate(hunk.lines) if pl.content == "line 2 changed")

    patch = line_patch(fd, hunk, {add_idx}, context=3)
    reparsed = parse_diff(patch)
    assert len(reparsed) == 1
    kinds = [pl.kind.name for pl in reparsed[0].hunks[0].lines]
    # The whole 6-line hunk is included: 4 context + 1 deletion + 1 addition.
    assert kinds.count("CONTEXT") == 4
    assert kinds.count("ADDITION") == 1
    assert kinds.count("DELETION") == 1


def test_line_patch_rejects_empty_selection() -> None:
    with pytest.raises(ValueError):
        line_patch(object(), object(), set())  # type: ignore[arg-type]
