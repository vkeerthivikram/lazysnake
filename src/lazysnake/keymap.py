"""Keymap: every lazysnake keybinding is data, so users can remap it.

Each panel declares ``ACTIONS`` — ``(action_id, description)`` pairs — and
:func:`bind_keys` generates the panel's ``BINDINGS`` from a key table.
The default table is :data:`DEFAULT_KEYS`; user config ``[keys]`` overlays
it (unknown entries are reported, duplicates within one panel are errors).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# panel_id -> action_id -> key
DEFAULT_KEYS: dict[str, dict[str, str]] = {
    "global": {
        "focus_files": "1",
        "focus_branches": "2",
        "focus_commits": "3",
        "focus_stash": "4",
        "focus_tags": "5",
        "focus_diff": "6",
        "fetch": "f",
        "pull": "p",
        "push": "P",
        "rebase_continue": "R",
        "rebase_abort": "K",
        "undo": "z",
        "redo": "ctrl+y",
        "context_down": "[",
        "context_up": "]",
        "recent_repos": "ctrl+r",
        "worktrees": "W",
        "submodules": "U",
        "grep": "ctrl+g",
        "refresh": "r",
        "quit": "q",
    },
    "files": {
        "toggle_stage": "space",
        "toggle_stage_all": "a",
        "commit": "c",
        "amend": "A",
        "discard": "d",
        "stash": "s",
        "stash_message": "S",
        "ignore": "i",
        "copy_path": "y",
        "open_editor": "E",
        "resolve_ours": "o",
        "resolve_theirs": "t",
        "resolve_union": "b",
        "enter_entry": "enter",
        "filter": "/",
    },
    "branches": {
        "checkout": "space",
        "new_branch": "n",
        "delete_branch": "d",
        "force_delete_branch": "D",
        "merge_branch": "M",
        "fast_forward": "f",
        "rebase_onto": "r",
        "set_upstream": "u",
    },
    "commits": {
        "checkout_commit": "space",
        "cherry_pick": "C",
        "revert": "v",
        "reword": "r",
        "fixup": "f",
        "drop_commit": "D",
        "squash_commit": "S",
        "move_up": "<",
        "move_down": ">",
        "copy_sha": "Y",
        "apply_patch": "w",
        "bisect_bad": "B",
        "bisect_good": "G",
        "bisect_reset": "ctrl+b",
        "todo_editor": "T",
        "filter": "/",
        "patch_mark": "m",
        "patch_apply": "a",
        "patch_cherry": "c",
        "patch_clear": "x",
    },
    "stash": {
        "apply_stash": "space",
        "pop_stash": "p",
        "drop_stash": "d",
    },
    "tags": {
        "checkout_tag": "space",
        "new_tag": "n",
        "delete_tag": "d",
    },
    "diff": {
        "stage_hunk": "space",
        "stage_line": "l",
        "scroll_left": "H",
        "scroll_right": "L",
    },
}


@dataclass
class Keymap:
    """Resolved key table with provenance for diagnostics."""

    keys: dict[str, dict[str, str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def key(self, panel: str, action: str) -> str:
        table = self.keys.get(panel, {})
        value = table.get(action)
        if value is None:
            raise KeyError(f"no key mapped for {panel}.{action}")
        return value

    @classmethod
    def defaults(cls) -> Keymap:
        return cls(
            keys={p: dict(actions) for p, actions in DEFAULT_KEYS.items()},
        )

    @classmethod
    def with_overrides(cls, overrides: dict[str, dict[str, str]]) -> Keymap:
        """Defaults overlaid by user config; unknown ids become warnings."""
        km = cls.defaults()
        for panel, actions in (overrides or {}).items():
            if panel not in km.keys:
                km.warnings.append(f"unknown key section [keys.{panel}]")
                continue
            for action, key in (actions or {}).items():
                if action not in km.keys[panel]:
                    km.warnings.append(f"unknown key binding {panel}.{action}")
                    continue
                km.keys[panel][action] = str(key)
        km.warnings.extend(km._validate())
        return km

    def _validate(self) -> list[str]:
        problems: list[str] = []
        for panel, actions in self.keys.items():
            seen: dict[str, str] = {}
            for action, key in actions.items():
                if key in seen:
                    problems.append(
                        f"[keys.{panel}] '{key}' is bound to both "
                        f"{seen[key]} and {action}"
                    )
                seen[key] = action
        return problems


def bind_keys(base: type, panel: str, keymap: Keymap) -> type:
    """Return a subclass of ``base`` with BINDINGS built from ``keymap``.

    ``base`` must declare ``ACTIONS``: a list of ``(action_id, description)``
    pairs, or ``(action_id, description, textual_action)`` when the id and
    the dispatched action differ (e.g. focus_files -> focus_panel('files')).
    The generated class keeps ``base``'s ``__name__`` so CSS selectors and
    imports keep working.
    """
    bindings: list[tuple[str, str, str]] = []
    for entry in base.ACTIONS:  # type: ignore[attr-defined]
        action_id, description = entry[0], entry[1]
        textual_action = entry[2] if len(entry) > 2 else action_id
        bindings.append((keymap.key(panel, action_id), textual_action, description))
    # Keep the base's __module__: Textual resolves relative CSS_PATH against
    # the class's module, and repr/pickle expect the real home.
    return type(
        base.__name__, (base,), {"BINDINGS": bindings, "__module__": base.__module__}
    )
