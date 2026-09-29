"""Keymap: defaults, overrides, validation, dynamic binding generation."""

from __future__ import annotations

from lazysnake.config import load_config
from lazysnake.keymap import DEFAULT_KEYS, Keymap, bind_keys
from lazysnake.ui.panels import FilesPanel


def test_defaults_cover_every_action() -> None:
    # Every panel with ACTIONS has a key for each action.
    from lazysnake.ui import panels

    for name in ("FilesPanel", "BranchesPanel", "CommitsPanel", "StashPanel",
                 "TagsPanel", "DiffView"):
        cls = getattr(panels, name)
        panel = {"FilesPanel": "files", "BranchesPanel": "branches",
                 "CommitsPanel": "commits", "StashPanel": "stash",
                 "TagsPanel": "tags", "DiffView": "diff"}[name]
        for entry in cls.ACTIONS:
            action_id = entry[0]
            assert action_id in DEFAULT_KEYS[panel], f"{panel}.{action_id} unmapped"


def test_override_and_unknown_ids(tmp_path) -> None:
    km = Keymap.with_overrides({"files": {"amend": "alt+A"}})
    assert km.key("files", "amend") == "alt+A"
    assert km.key("files", "commit") == "c"  # untouched

    km2 = Keymap.with_overrides({"files": {"nope": "x"}, "bogus": {"x": "y"}})
    assert any("files.nope" in w for w in km2.warnings)
    assert any("keys.bogus" in w for w in km2.warnings)


def test_duplicate_key_within_panel_rejected() -> None:
    km = Keymap.with_overrides({"files": {"commit": "d", "discard": "d"}})
    assert any("bound to both" in w for w in km.warnings)


def test_bind_keys_generates_working_subclass() -> None:
    km = Keymap.with_overrides({"files": {"commit": "K"}})
    cls = bind_keys(FilesPanel, "files", km)
    assert cls.__name__ == "FilesPanel"
    by_action = {action: key for key, action, _ in cls.BINDINGS}
    assert by_action["commit"] == "K"
    # Subclass relationship: instances pass isinstance checks.
    assert issubclass(cls, FilesPanel)


async def test_remapped_key_actually_fires(repo) -> None:
    from lazysnake.git.runner import Git
    from lazysnake.git.status import parse_status
    from lazysnake.ui.app import LazysnakeApp

    repo.write("README.md", "changed\n")
    km = Keymap.with_overrides({"files": {"toggle_stage": "u"}})
    app = LazysnakeApp(Git(repo.root), keymap=km)
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await pilot.press("u")  # remapped stage key
        await pilot.pause()
        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        by_path = {f.path: f for f in snap.files}
        assert by_path["README.md"].display == "M "
        # The old key no longer triggers staging.
        repo.write("second.txt", "s\n")
        await app.refresh_state(force=True).wait()
        await pilot.press("space")
        await pilot.pause()
        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        by_path = {f.path: f for f in snap.files}
        assert by_path["second.txt"].display == "??"


def test_config_keys_section(tmp_path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        '[keys.files]\ncommit = "K"\n\n[keys.global]\nundo = "ctrl+z"\n'
    )
    cfg = load_config(target)
    assert cfg.key_overrides == {
        "files": {"commit": "K"},
        "global": {"undo": "ctrl+z"},
    }


def test_global_action_id_maps_to_textual_action() -> None:
    from lazysnake.ui.app import LazysnakeApp

    by_action = {entry[2]: entry[0] for entry in LazysnakeApp.ACTIONS}
    assert by_action["focus_panel('files')"] == "focus_files"
    km = Keymap.with_overrides({"global": {"focus_files": "F1"}})
    app_cls = bind_keys(LazysnakeApp, "global", km)
    bindings = {action: key for key, action, _ in app_cls.BINDINGS}
    assert bindings["focus_panel('files')"] == "F1"
