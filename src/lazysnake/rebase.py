"""Interactive-rebase plumbing via the ``GIT_SEQUENCE_EDITOR`` trick.

The UI writes a plan (commit sha → todo verb) to a JSON file and launches
``git rebase -i`` with ``GIT_SEQUENCE_EDITOR`` pointed at
``python -m lazysnake.sequence_editor <plan>``.  Git invokes that "editor"
on the todo file; we rewrite the ``pick`` lines per the plan and exit, and
git performs the rebase non-interactively.

Plans may also use the pseudo-verbs ``move-up`` / ``move-down``, which swap
the matching todo entry with its neighbour instead of changing its verb.
Rewords additionally pass a message through ``python -m lazysnake.msg_editor``
as ``GIT_EDITOR``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lazysnake.git.runner import Git

SUPPORTED_OPS = ("drop", "squash", "fixup", "edit", "reword", "move-up", "move-down")
TODO_VERBS = ("pick", "reword", "edit", "squash", "fixup", "drop")


@dataclass
class TodoEntry:
    """One line of an interactive-rebase todo."""

    sha: str
    subject: str
    verb: str = "pick"
    original_pos: int = 0  # index in the original (oldest-first) todo


@dataclass
class TodoPlan:
    """A user-editable rebase plan, oldest commit first.

    ``squash``/``fixup`` fold into the *previous* entry; moves reorder
    entries directly. ``build`` returns the verb ops and the desired sha
    order that ``rebase_with_ops`` carries into the todo rewrite.
    """

    entries: list[TodoEntry] = field(default_factory=list)
    reword_message: str | None = None
    reword_sha: str | None = None

    def set_verb(self, index: int, verb: str) -> None:
        if verb not in TODO_VERBS:
            raise ValueError(f"unknown todo verb: {verb}")
        if not 0 <= index < len(self.entries):
            return
        entry = self.entries[index]
        if verb == "reword" and self.reword_sha not in (None, entry.sha):
            return  # one reword per run (single GIT_EDITOR message)
        entry.verb = verb
        if verb == "reword":
            self.reword_sha = entry.sha
            if self.reword_message is None:
                self.reword_message = entry.subject

    def move(self, index: int, direction: int) -> None:
        """Swap entry at ``index`` with its neighbour (-1 up, +1 down)."""
        target = index + direction
        if not 0 <= target < len(self.entries):
            return
        if direction < 0 and self.entries[index].verb in ("squash", "fixup"):
            return  # squash cannot move above its anchor
        self.entries[index], self.entries[target] = self.entries[target], self.entries[index]

    def build(self) -> tuple[dict[str, str], list[str]]:
        """Return ``(verb_ops, sha_order)`` for ``rebase_with_ops``."""
        ops = {e.sha: e.verb for e in self.entries if e.verb != "pick"}
        order = [e.sha for e in self.entries]
        return ops, order


def _sequence_editor_command(plan_path: Path) -> str:
    return f'"{sys.executable}" -m lazysnake.sequence_editor "{plan_path}"'


def _message_editor_command(message_path: Path) -> str:
    return f'"{sys.executable}" -m lazysnake.msg_editor "{message_path}"'


async def rebase_with_ops(
    git: Git,
    base: str | list[str],
    ops: dict[str, str],
    *,
    message: str | None = None,
    order: list[str] | None = None,
) -> str:
    """Rebase interactively from ``base``, rewriting commits per ``ops``.

    ``base`` is passed to ``git rebase -i`` after ``base_args`` conversion —
    normally the parent of the oldest commit being rewritten (``<sha>^``).
    For squash/fixup the parent itself must appear in the todo as the
    anchor, so pass the grandparent (``<sha>^^``) or ``["--root"]``.

    ``message`` (with a ``reword`` op) is piped through a msg-editor hook so
    the reworded commit gets exactly that message.  ``order`` gives the
    desired sha sequence for the whole todo (used by the todo editor).
    """
    bad = set(ops.values()) - set(SUPPORTED_OPS)
    if bad:
        raise ValueError(f"unsupported rebase ops: {sorted(bad)}")

    base_args = [base] if isinstance(base, str) else list(base)

    plan_path = _tempfile(suffix=".json")
    plan: dict[str, Any] = {"ops": ops}
    if order is not None:
        plan["order"] = order
    plan_path.write_text(json.dumps(plan))
    message_path = None
    if message is not None:
        message_path = _tempfile(suffix=".msg")
        message_path.write_text(message)

    env_extra = {
        "GIT_SEQUENCE_EDITOR": _sequence_editor_command(plan_path),
        "GIT_EDITOR": _message_editor_command(message_path) if message_path else "true",
    }
    try:
        return await git.run("rebase", "-i", *base_args, env_extra=env_extra, timeout=300)
    finally:
        plan_path.unlink(missing_ok=True)
        if message_path is not None:
            message_path.unlink(missing_ok=True)


def _tempfile(*, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(
        "w", suffix=suffix, prefix="lazysnake-rebase-", delete=False
    ) as fh:
        return Path(fh.name)


def rewrite_todo(todo_text: str, ops: dict[str, str], order: list[str] | None = None) -> str:
    """Rewrite ``pick`` lines whose sha matches a planned op.

    Used by ``sequence_editor``; exposed here for testing.  Todo shas are
    abbreviated, so ops keys match by prefix.  ``move-up``/``move-down``
    swap the matching entry with the previous/next entry.  ``order``, when
    given, defines the final entry sequence outright.
    """
    lines = todo_text.splitlines()
    for sha, op in ops.items():
        idx = _find_entry(lines, sha)
        if idx is None:
            continue
        if op == "move-up":
            swap = _neighbour_entry(lines, idx, -1)
            if swap is not None:
                lines[idx], lines[swap] = lines[swap], lines[idx]
        elif op == "move-down":
            swap = _neighbour_entry(lines, idx, +1)
            if swap is not None:
                lines[idx], lines[swap] = lines[swap], lines[idx]
        else:
            stripped = lines[idx].strip()
            parts = stripped.split(maxsplit=2)
            rest = parts[2] if len(parts) >= 3 else ""
            todo_sha = parts[1] if len(parts) >= 2 else ""
            lines[idx] = f"{op} {todo_sha} {rest}".rstrip()
    if order is not None:
        lines = _reorder_entries(lines, order)
    trailing = "\n" if todo_text.endswith("\n") else ""
    return "\n".join(lines) + trailing


def _reorder_entries(lines: list[str], order: list[str]) -> list[str]:
    """Sort todo entries into ``order`` (prefix-matched), comments on top."""
    first = next((i for i, line in enumerate(lines) if _is_entry(line)), len(lines))
    head = lines[:first]
    rest = lines[first:]
    entries = [line for line in rest if _is_entry(line)]
    tail = [line for line in rest if not _is_entry(line)]

    def rank(line: str) -> int:
        parts = line.strip().split(maxsplit=2)
        sha = parts[1] if len(parts) >= 2 else ""
        for pos, want in enumerate(order):
            if sha and (want.startswith(sha) or sha.startswith(want)):
                return pos
        return len(order)

    entries.sort(key=rank)
    return head + entries + tail


# Verbs that can open a todo entry, long or single-letter form.
_ENTRY_PREFIXES = (
    "pick ",
    "p ",
    "drop ",
    "d ",
    "squash ",
    "s ",
    "fixup ",
    "f ",
    "edit ",
    "e ",
    "reword ",
    "r ",
)


def _is_entry(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(_ENTRY_PREFIXES) and not stripped.startswith("#")


def _find_entry(lines: list[str], sha: str) -> int | None:
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _is_entry(stripped):
            parts = stripped.split(maxsplit=2)
            todo_sha = parts[1] if len(parts) >= 2 else ""
            if todo_sha and (sha.startswith(todo_sha) or todo_sha.startswith(sha)):
                return i
    return None


def _neighbour_entry(lines: list[str], idx: int, direction: int) -> int | None:
    i = idx + direction
    while 0 <= i < len(lines):
        if _is_entry(lines[i]):
            return i
        i += direction
    return None
