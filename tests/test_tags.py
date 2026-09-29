"""Tag parser and app-level tag flows."""

from __future__ import annotations

from lazysnake.git.runner import Git
from lazysnake.git.tags import FORMAT as TAG_FORMAT
from lazysnake.git.tags import parse_tags
from lazysnake.ui.app import LazysnakeApp


def test_parse_tags_fixture() -> None:
    sep = "\x1f"
    data = "\n".join(
        [
            f"v1.0{sep}tagobj1{sep}commit1{sep}release one{sep}2026-09-01",
            f"v2.0{sep}commit2{sep}{sep}{sep}2026-09-20",
        ]
    )
    v1, v2 = parse_tags(data)
    assert v1.name == "v1.0"
    assert v1.annotated  # dereferenced commit present
    assert v1.sha == "commit1"
    assert v1.subject == "release one"
    assert v1.when == "2026-09-01"
    assert not v2.annotated
    assert v2.sha == "commit2"  # lightweight: object is the commit


async def test_parse_live_tags(repo) -> None:
    git = Git(repo.root)
    repo.git("tag", "lightweight")
    repo.git("tag", "-a", "v1.0", "-m", "first release")

    tags = parse_tags(await git.run("for-each-ref", "refs/tags", f"--format={TAG_FORMAT}"))
    by_name = {t.name: t for t in tags}
    assert set(by_name) == {"lightweight", "v1.0"}
    assert by_name["lightweight"].sha == repo.git("rev-parse", "HEAD").strip()
    assert not by_name["lightweight"].annotated
    assert by_name["v1.0"].annotated
    assert by_name["v1.0"].subject == "first release"
    # Both resolve to the same commit.
    assert by_name["v1.0"].sha == by_name["lightweight"].sha


async def test_tag_app_flows(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert app.tags == []

        await app.create_tag("v0.9").wait()
        await pilot.pause()
        assert [t.name for t in app.tags] == ["v0.9"]

        tag = app.tags[0]
        await app.checkout_detached(tag.sha, label=tag.name).wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == tag.sha
        from lazysnake.git.status import parse_status

        head_state = parse_status(
            await app.git.run("status", "--porcelain=v2", "--branch", "-z")
        ).branch
        assert head_state == "(detached)"

        await app.delete_tag(tag).wait()
        await pilot.pause()
        assert app.tags == []
        assert repo.git("tag").strip() == ""
