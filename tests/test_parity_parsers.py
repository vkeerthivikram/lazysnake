"""Parsers for the parity batch: graph lanes, worktrees, union merge, recent."""

from __future__ import annotations

from pathlib import Path

from lazysnake.git.log import GRAPH_FORMAT, parse_log_graph
from lazysnake.git.merge import union_resolve
from lazysnake.git.runner import Git
from lazysnake.git.worktree import parse_worktrees
from lazysnake.recent import load_recent, record_recent


async def _graph(git: Git):
    data = await git.run("log", "--graph", f"--pretty=format:{GRAPH_FORMAT}")
    return parse_log_graph(data)


async def test_graph_parse_linear(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.commit_all("second")
    graph = await _graph(Git(repo.root))
    assert len(graph) == 2
    assert all(g.lanes.startswith("*") for g in graph)
    assert graph[0].commit.subject == "second"


async def test_graph_parse_with_merge(repo) -> None:
    repo.git("switch", "-c", "side")
    repo.write("side.txt", "s\n")
    repo.commit_all("side work")
    repo.git("switch", "main")
    repo.write("main.txt", "m\n")
    repo.commit_all("main work")  # diverge so the merge is a true merge
    repo.git("merge", "--no-edit", "side")

    graph = await _graph(Git(repo.root))
    subjects = [g.commit.subject for g in graph]
    assert "side work" in subjects
    assert any("initial commit" in s for s in subjects)
    # Merge topology produces multi-line lane art (fan-out rows).
    assert any("\n" in g.lanes for g in graph)
    assert any(ch in g.lanes for g in graph for ch in "\\|/")


async def test_worktree_parse_and_flow(repo) -> None:
    git = Git(repo.root)
    trees = parse_worktrees(await git.run("worktree", "list", "--porcelain"))
    assert len(trees) == 1
    assert trees[0].is_main
    assert trees[0].branch.endswith("main")
    assert not trees[0].bare
    assert trees[0].branch_short == "main"

    # Add a worktree and re-parse.
    repo.git("worktree", "add", "../extra-wt", "-b", "extra")
    trees = parse_worktrees(await git.run("worktree", "list", "--porcelain"))
    assert len(trees) == 2
    extra = next(t for t in trees if t.branch.endswith("extra"))
    assert not extra.is_main
    assert Path(extra.path).exists()


def test_union_resolve_fixture() -> None:
    content = (
        "common top\n"
        "<<<<<<< HEAD\n"
        "our line\n"
        "=======\n"
        "their line\n"
        ">>>>>>> branch\n"
        "common bottom\n"
    )
    merged, count = union_resolve(content)
    assert count == 1
    assert "our line\ntheir line\n" in merged
    assert "<<<<<<<" not in merged and ">>>>>>>" not in merged
    assert merged.startswith("common top\n")
    assert merged.endswith("common bottom\n")

    clean, zero = union_resolve("no markers here\n")
    assert clean == "no markers here\n"
    assert zero == 0


async def test_union_resolve_live_conflict(repo) -> None:
    repo.git("switch", "-c", "side")
    repo.write("README.md", "their line\n")
    repo.commit_all("their change")
    repo.git("switch", "main")
    repo.write("README.md", "our line\n")
    repo.commit_all("our change")
    repo.git("merge", "--no-edit", "side", check=False)  # conflicts: exit 1 is expected

    conflicted = (repo.root / "README.md").read_text()
    assert "<<<<<<<" in conflicted
    merged, count = union_resolve(conflicted)
    assert count == 1
    assert "our line\ntheir line\n" in merged


def test_recent_store(tmp_path: Path) -> None:
    store = tmp_path / "repos.json"
    assert load_recent(store) == []

    record_recent("/a/b", store)
    record_recent("/c/d", store)
    record_recent("/a/b", store)  # dedupe, moves to front
    assert load_recent(store) == ["/a/b", "/c/d"]

    for i in range(15):
        record_recent(f"/repo{i}", store)
    entries = load_recent(store)
    assert len(entries) == 10
    assert entries[0] == "/repo14"
