"""Modal dialogs: danger confirmations, commit editor, todo editor, pickers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, TextArea

from lazysnake.git.submodule import Submodule
from lazysnake.git.worktree import Worktree
from lazysnake.rebase import TodoPlan

MAX_GREP_MATCHES = 200


class ConfirmScreen(ModalScreen[bool]):
    """Ask before destructive git operations. Dismisses with True/False."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"),
        ("y", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
    ]

    def __init__(self, prompt: str, *, danger: bool = True) -> None:
        super().__init__()
        self.prompt = prompt
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt, id="confirm-prompt")
            with Horizontal(id="confirm-buttons"):
                yield Button(
                    "Confirm", id="confirm-btn", variant="error" if self.danger else "default"
                )
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-btn")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class CommitScreen(ModalScreen[str]):
    """Editor for the commit message. Dismisses with the text or None."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+enter", "submit", "Commit"),
    ]
    AUTO_FOCUS = "#commit-msg"

    def __init__(self, *, prefill: str = "") -> None:
        super().__init__()
        self.prefill = prefill

    def compose(self) -> ComposeResult:
        with Vertical(id="commit-box"):
            yield Label("Commit message:", id="commit-label")
            yield TextArea(self.prefill, id="commit-msg")
            with Horizontal(id="commit-buttons"):
                yield Button("Commit", id="commit-btn", variant="success")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "commit-btn":
            self.action_submit()
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        text = self.query_one("#commit-msg", TextArea).text.strip()
        if text:
            self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class InputScreen(ModalScreen[str]):
    """Single-line text input (e.g. new branch name). Dismisses with text or None."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]
    AUTO_FOCUS = "#input-field"

    def __init__(self, prompt: str, *, placeholder: str = "", prefill: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.placeholder = placeholder
        self.prefill = prefill

    def compose(self) -> ComposeResult:
        with Vertical(id="input-box"):
            yield Label(self.prompt, id="input-label")
            yield Input(value=self.prefill, placeholder=self.placeholder, id="input-field")
            with Horizontal(id="input-buttons"):
                yield Button("OK", id="input-ok", variant="success")
                yield Button("Cancel", id="cancel-btn")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "input-ok":
            self._submit(self.query_one("#input-field", Input).value)
        else:
            self.dismiss(None)

    def _submit(self, value: str) -> None:
        value = value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _TodoRow(ListItem):
    def __init__(self, plan: TodoPlan, index: int) -> None:
        self.plan_index = index
        entry = plan.entries[index]
        verb_style = {
            "pick": "cyan",
            "reword": "yellow",
            "edit": "magenta",
            "squash": "green",
            "fixup": "green",
            "drop": "red",
        }[entry.verb]
        text = Text.assemble(
            (f"{entry.verb:<6}", verb_style),
            (entry.sha[:8] + " ", "dim yellow"),
            (entry.subject, ""),
        )
        super().__init__(Label(text))


@dataclass
class RebaseTodoResult:
    """What the todo editor decided: verbs, order, and a reword message."""

    ops: dict[str, str]
    order: list[str]
    message: str | None = None


class RebaseTodoScreen(ModalScreen[RebaseTodoResult]):
    """Full interactive-rebase todo editor.

    Dismisses with a :class:`RebaseTodoResult` on run, or ``None``.
    Keys: p/r/e/s/f/d set verbs, </> move, c or enter runs, escape cancels.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "cancel"),
        ("p", "verb('pick')", "pick"),
        ("r", "verb('reword')", "reword"),
        ("e", "verb('edit')", "edit"),
        ("s", "verb('squash')", "squash"),
        ("f", "verb('fixup')", "fixup"),
        ("d", "verb('drop')", "drop"),
        ("<", "move_up", "move up"),
        (">", "move_down", "move down"),
        ("c", "run", "run rebase"),
        ("enter", "run", "run rebase"),
    ]

    def __init__(self, plan: TodoPlan) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        with Vertical(id="todo-box"):
            yield Label(
                "Rebase plan (oldest first): p pick · r reword · e edit · "
                "s squash · f fixup · d drop · </> move · c run",
                id="todo-label",
            )
            yield ListView(id="todo-list")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one("#todo-list", ListView).focus()

    def _list(self) -> ListView:
        return self.query_one("#todo-list", ListView)

    def _rebuild(self) -> None:
        view = self._list()
        view.clear()
        for i in range(len(self.plan.entries)):
            view.append(_TodoRow(self.plan, i))
        if self.plan.entries:
            view.index = 0

    def _current(self) -> int | None:
        idx = self._list().index
        if idx is not None and 0 <= idx < len(self.plan.entries):
            return idx
        return None

    def action_verb(self, verb: str) -> None:
        idx = self._current()
        if idx is not None:
            self.plan.set_verb(idx, verb)
            self._rebuild()

    def action_move_up(self) -> None:
        idx = self._current()
        if idx is not None:
            self.plan.move(idx, -1)
            self._rebuild()

    def action_move_down(self) -> None:
        idx = self._current()
        if idx is not None:
            self.plan.move(idx, +1)
            self._rebuild()

    def action_run(self) -> None:
        ops, order = self.plan.build()
        self.dismiss(RebaseTodoResult(ops=ops, order=order, message=self.plan.reword_message))

    def action_cancel(self) -> None:
        self.dismiss(None)


class RecentReposScreen(ModalScreen[str]):
    """Pick a recently opened repository. Dismisses with its path or None."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "cancel"),
        ("enter", "choose", "open"),
    ]

    def __init__(self, paths: list[str], current: str) -> None:
        super().__init__()
        self.paths = [p for p in paths if p != current][:9] or [p for p in paths][:9]
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="recent-box"):
            yield Label("Recent repositories (enter to open, esc to cancel):")
            yield ListView(id="recent-list")

    def on_mount(self) -> None:
        view = self.query_one("#recent-list", ListView)
        for path in self.paths:
            view.append(_PathRow(path))
        if self.paths:
            view.index = 0
        view.focus()

    def action_choose(self) -> None:
        view = self.query_one("#recent-list", ListView)
        idx = view.index
        if idx is not None and 0 <= idx < len(self.paths):
            self.dismiss(self.paths[idx])

    def action_cancel(self) -> None:
        self.dismiss(None)


class _PathRow(ListItem):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(Label(Text.assemble(("  ", "dim"), (path, "cyan"))))


class _GrepRow(ListItem):
    """One grep match: ``path:line:text``."""

    def __init__(self, path: str, line_no: int, text: str) -> None:
        self.path = path
        self.line_no = line_no
        super().__init__(
            Label(
                Text.assemble(
                    (f"{path}:{line_no} ", "yellow"),
                    (text, ""),
                )
            )
        )


@dataclass
class GrepMatch:
    """One selected grep result: where to jump, and whether to stage first."""

    path: str
    line: int
    text: str
    stage: bool = False


class GrepScreen(ModalScreen[GrepMatch]):
    """Grep results. Dismisses with a :class:`GrepMatch` on enter (``stage``
    False) or space (``stage`` True), or ``None``."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "cancel"),
        ("enter", "jump", "go to match"),
        ("space", "jump_stage", "go + stage file"),
    ]

    def __init__(
        self, matches: list[tuple[str, int, str]], term: str, *, truncated: bool = False
    ) -> None:
        super().__init__()
        self.matches = matches[:MAX_GREP_MATCHES]
        self.truncated = truncated or len(matches) > MAX_GREP_MATCHES
        self.term = term

    def compose(self) -> ComposeResult:
        with Vertical(id="grep-box"):
            count = f"{len(self.matches)} matches"
            if self.truncated:
                count += f" (truncated to first {MAX_GREP_MATCHES}; more omitted)"
            yield Label(f"grep '{self.term}' — {count} (enter jump · space jump+stage · esc):")
            yield ListView(id="grep-list")

    def on_mount(self) -> None:
        view = self.query_one("#grep-list", ListView)
        for path, line_no, text in self.matches:
            view.append(_GrepRow(path, line_no, text))
        if self.matches:
            view.index = 0
        view.focus()

    def _selection(self, *, stage: bool) -> GrepMatch | None:
        view = self.query_one("#grep-list", ListView)
        idx = view.index
        if idx is None or not 0 <= idx < len(self.matches):
            return None
        path, line_no, text = self.matches[idx]
        return GrepMatch(path=path, line=line_no, text=text, stage=stage)

    def action_jump(self) -> None:
        self.dismiss(self._selection(stage=False))

    def action_jump_stage(self) -> None:
        self.dismiss(self._selection(stage=True))

    def action_cancel(self) -> None:
        self.dismiss(None)


class _WorktreeRow(ListItem):
    def __init__(self, wt: Worktree) -> None:
        self.worktree = wt
        main_marker = "*" if wt.is_main else " "
        style = "dim" if wt.bare else "cyan"
        super().__init__(
            Label(
                Text.assemble(
                    (main_marker + " ", "green"),
                    (wt.path + " ", style),
                    (f"[{wt.branch_short}]", "yellow"),
                )
            )
        )


class _SubmoduleRow(ListItem):
    def __init__(self, sub: Submodule) -> None:
        self.submodule = sub
        flag_style = {"+": "yellow", "-": "red", "U": "red"}.get(sub.flag, "green")
        super().__init__(
            Label(
                Text.assemble(
                    (sub.path + "  ", "cyan"),
                    (f"[{sub.state}]", flag_style),
                )
            )
        )


@dataclass
class SubmoduleResult:
    """Chosen submodule action: ``enter``/``update``/``deinit`` carry a
    ``path``; ``add`` does not."""

    action: str
    path: str | None = None


class SubmodulesScreen(ModalScreen[SubmoduleResult]):
    """Submodule manager. Dismisses with a :class:`SubmoduleResult` or None."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "cancel"),
        ("enter", "enter_sub", "open"),
        ("u", "update", "update --init"),
        ("a", "add", "add submodule"),
        ("d", "deinit", "deinit"),
    ]

    def __init__(self, subs: list[Submodule]) -> None:
        super().__init__()
        self.subs = subs

    def compose(self) -> ComposeResult:
        with Vertical(id="submodule-box"):
            yield Label("Submodules (enter open · u update · a add · d deinit · esc):")
            yield ListView(id="submodule-list")

    def on_mount(self) -> None:
        view = self.query_one("#submodule-list", ListView)
        for sub in self.subs:
            view.append(_SubmoduleRow(sub))
        if self.subs:
            view.index = 0
        view.focus()

    def _selected_path(self) -> str | None:
        view = self.query_one("#submodule-list", ListView)
        idx = view.index
        if idx is not None and 0 <= idx < len(self.subs):
            return self.subs[idx].path
        return None

    def action_enter_sub(self) -> None:
        path = self._selected_path()
        if path:
            self.dismiss(SubmoduleResult(action="enter", path=path))

    def action_update(self) -> None:
        self.dismiss(SubmoduleResult(action="update"))

    def action_add(self) -> None:
        self.dismiss(SubmoduleResult(action="add"))

    def action_deinit(self) -> None:
        path = self._selected_path()
        if path:
            self.dismiss(SubmoduleResult(action="deinit", path=path))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass
class WorktreeResult:
    """Chosen worktree action: ``switch``/``delete`` carry a ``path``;
    ``add`` does not."""

    action: str
    path: str | None = None


class WorktreeScreen(ModalScreen[WorktreeResult]):
    """Worktree browser. Dismisses with a :class:`WorktreeResult` or None."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "cancel"),
        ("enter", "switch", "open"),
        ("n", "add", "new worktree"),
        ("d", "delete", "remove"),
    ]

    def __init__(self, worktrees: list[Worktree]) -> None:
        super().__init__()
        self.worktrees = worktrees

    def compose(self) -> ComposeResult:
        with Vertical(id="worktree-box"):
            yield Label("Worktrees (enter open · n new · d remove · esc cancel):")
            yield ListView(id="worktree-list")

    def on_mount(self) -> None:
        view = self.query_one("#worktree-list", ListView)
        for wt in self.worktrees:
            view.append(_WorktreeRow(wt))
        if self.worktrees:
            view.index = 0
        view.focus()

    def _selected_path(self) -> str | None:
        view = self.query_one("#worktree-list", ListView)
        idx = view.index
        if idx is not None and 0 <= idx < len(self.worktrees):
            return self.worktrees[idx].path
        return None

    def action_switch(self) -> None:
        path = self._selected_path()
        if path:
            self.dismiss(WorktreeResult(action="switch", path=path))

    def action_add(self) -> None:
        self.dismiss(WorktreeResult(action="add"))

    def action_delete(self) -> None:
        path = self._selected_path()
        if path:
            self.dismiss(WorktreeResult(action="delete", path=path))

    def action_cancel(self) -> None:
        self.dismiss(None)
