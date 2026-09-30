"""App flows for the parity batch: undo/redo, filters, union, bisect, patch."""

from __future__ import annotations

from lazysnake.config import Config, CustomCommand
from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status
from lazysnake.rebase import TodoEntry, TodoPlan
from lazysnake.ui.app import LazysnakeApp


async def test_undo_redo_stack(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.git("add", "-A")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        initial = repo.git("rev-parse", "HEAD").strip()

        await app.commit("first").wait()
        await pilot.pause()
        first = repo.git("rev-parse", "HEAD").strip()
        repo.write("b.txt", "b\n")
        repo.git("add", "-A")
        await app.commit("second").wait()
        await pilot.pause()
        second = repo.git("rev-parse", "HEAD").strip()
        assert initial != first != second

        await app.undo().wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == first
        await app.undo().wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == initial

        await app.redo().wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == first

        # A new action clears the redo path.
        repo.write("c.txt", "c\n")
        repo.git("add", "-A")
        await app.commit("third").wait()
        await pilot.pause()
        await app.redo().wait()
        await pilot.pause()
        assert repo.git("log", "-1", "--pretty=%s").strip() == "third"


async def test_files_filter(repo) -> None:
    repo.write("alpha.py", "a\n")
    repo.write("beta.py", "b\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert len(app.snapshot.files) == 2

        app.set_filter("files", "alpha")
        await pilot.pause()
        paths = [item.entry.path for item in app.files_panel.children if hasattr(item, "entry")]
        assert paths == ["alpha.py"]
        assert "alpha" in app.files_panel.border_title

        app.set_filter("files", None)
        await pilot.pause()
        paths = [item.entry.path for item in app.files_panel.children if hasattr(item, "entry")]
        assert set(paths) == {"alpha.py", "beta.py"}


async def test_commits_filter(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.commit_all("feat: add a")
    repo.write("b.txt", "b\n")
    repo.commit_all("fix: repair b")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert len(app.commits) == 3

        app.set_filter("commits", "feat")
        await pilot.pause()
        subjects = [
            item.commit.subject for item in app.commits_panel.children if hasattr(item, "commit")
        ]
        assert subjects == ["feat: add a"]

        app.set_filter("commits", "zzz-no-match")
        await pilot.pause()
        assert app.commits_panel.index is None  # "(no matches)" header only


async def test_union_conflict_flow(repo) -> None:
    repo.git("switch", "-c", "side")
    repo.write("README.md", "their line\n")
    repo.commit_all("their change")
    repo.git("switch", "main")
    repo.write("README.md", "our line\n")
    repo.commit_all("our change")
    repo.git("merge", "--no-edit", "side", check=False)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        conflicted = snap.conflict_files[0]

        await app.resolve_conflict_union(conflicted).wait()
        await pilot.pause()

        content = (repo.root / "README.md").read_text()
        assert "<<<<<<<" not in content
        assert "our line" in content and "their line" in content
        snap2 = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        assert not snap2.in_conflict


async def test_bisect_flow(repo) -> None:
    # Three commits: good, bad, bad. Bisect should land on the first bad one.
    repo.write("f.txt", "1\n")
    repo.commit_all("good change")
    repo.write("f.txt", "2\n")
    repo.commit_all("bad change")
    repo.write("f.txt", "3\n")
    repo.commit_all("another bad")
    good_sha = repo.git("log", "--pretty=%H", "--grep=^good change$", "-1").strip()

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        bad = app.commits[0]
        await app.bisect_mark(bad, "bad").wait()
        await pilot.pause()
        assert app.snapshot.bisecting

        good = next(c for c in app.commits if c.sha == good_sha)
        await app.bisect_mark(good, "good").wait()
        await pilot.pause()
        # With one good and one bad, git checks out the culprit: "bad change".
        assert repo.git("log", "-1", "--pretty=%s").strip() == "bad change"

        await app.bisect_reset().wait()
        await pilot.pause()
        assert not app.snapshot.bisecting
        assert repo.git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


async def test_apply_commit_patch(repo) -> None:
    repo.git("switch", "-c", "donor")
    repo.write("gift.txt", "patched in\n")
    repo.commit_all("gift")
    gift_sha = repo.git("rev-parse", "HEAD").strip()
    repo.git("switch", "main")
    assert not (repo.root / "gift.txt").exists()

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        from lazysnake.git.log import Commit

        gift = Commit(
            sha=gift_sha, short_sha=gift_sha[:7], author="T", timestamp=0, subject="gift", refs=""
        )
        await app.apply_commit_patch(gift).wait()
        await pilot.pause()
        assert (repo.root / "gift.txt").read_text() == "patched in\n"
        # Applied to worktree only: still untracked, not committed.
        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        assert any(f.path == "gift.txt" and f.untracked for f in snap.files)


async def test_custom_command_runs(repo) -> None:
    config = Config(
        custom_commands=(
            CustomCommand(key="!", command="touch custom-ran.txt"),
            CustomCommand(key="X", command="false", confirm=False),
        )
    )
    app = LazysnakeApp(Git(repo.root), config=config)
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        # The key is unbound elsewhere -> on_key dispatches it.
        await pilot.press("!")
        await pilot.pause()
        assert (repo.root / "custom-ran.txt").exists()


def test_todo_plan_logic() -> None:
    plan = TodoPlan(
        entries=[
            TodoEntry(sha="a" * 40, subject="one", original_pos=0),
            TodoEntry(sha="b" * 40, subject="two", original_pos=1),
            TodoEntry(sha="c" * 40, subject="three", original_pos=2),
        ]
    )
    plan.set_verb(1, "squash")
    assert plan.entries[1].verb == "squash"

    # Squash cannot move above its anchor.
    plan.move(1, -1)
    assert [e.subject for e in plan.entries] == ["one", "two", "three"]

    plan.move(2, -1)  # three above two
    assert [e.subject for e in plan.entries] == ["one", "three", "two"]

    ops, order = plan.build()
    assert ops == {"b" * 40: "squash"}
    assert order == ["a" * 40, "c" * 40, "b" * 40]

    # Reword only once per run.
    plan.set_verb(0, "reword")
    plan.set_verb(2, "reword")
    rewords = [e for e in plan.entries if e.verb == "reword"]
    assert len(rewords) == 1


async def test_todo_editor_screen_flow(repo, eventually) -> None:
    for n in ("one", "two", "three"):
        repo.write(f"{n}.txt", f"{n}\n")
        repo.commit_all(f"commit {n}")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        # Open the todo editor from the OLDEST of the three commits.
        oldest = next(c for c in app.commits if c.subject == "commit one")
        app.request_todo_editor(oldest)
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        # Drop the first todo entry, then run the rebase.
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("c")
        await eventually(
            lambda: (
                [c.subject for c in app.commits] == ["commit three", "commit two", "initial commit"]
            )
        )
        subjects = [c.subject for c in app.commits]
        assert subjects == ["commit three", "commit two", "initial commit"]
