"""Hunk- and line-level staging by forging patches for ``git apply``.

``git apply --cached`` moves changes into the index without touching the
worktree, and ``--reverse --cached`` takes them back out — that pair gives us
lazygit-style partial staging on any hunk or single line.
"""

from __future__ import annotations

from lazysnake.git.diff import FileDiff, Hunk, LineKind, PatchLine


def hunk_patch(fd: FileDiff, hunk: Hunk) -> str:
    """An appliable patch containing only ``hunk`` of ``fd``."""
    return fd.to_patch([hunk])


def line_patch(fd: FileDiff, hunk: Hunk, selected: set[int], context: int = 3) -> str:
    """An appliable patch for selected line indexes of ``hunk``.

    ``selected`` indexes into ``hunk.lines``.  Context lines around each
    selected line are included (up to ``context`` of them per side) so the
    patch anchors correctly; hunk headers are recomputed.
    """
    if not selected:
        raise ValueError("no valid lines selected")
    lines = hunk.lines
    if not any(0 <= i < len(lines) for i in selected):
        raise ValueError("no valid lines selected")

    include = _expand_context(lines, selected, context)
    kept = [line for i, line in enumerate(lines) if i in include]

    old_start, old_count = _range_for(kept, old=True, fallback=hunk.old_start)
    new_start, new_count = _range_for(kept, old=False, fallback=hunk.new_start)
    section = f" {hunk.section}" if hunk.section else ""
    header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{section}"

    body = "\n".join(_render(pl) for pl in kept)
    inner = f"{header}\n{body}"

    parts = [fd.raw_git_header, *fd.header_lines, inner]
    return "\n".join(p for p in parts if p) + "\n"


def _expand_context(lines: list[PatchLine], selected: set[int], context: int) -> set[int]:
    include: set[int] = set()
    for i in selected:
        if not 0 <= i < len(lines):
            continue
        # Only +/- lines drive selection; context neighbours surround them.
        if lines[i].kind is LineKind.ADDITION or lines[i].kind is LineKind.DELETION:
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                include.add(j)
    return include


def _range_for(kept: list[PatchLine], *, old: bool, fallback: int) -> tuple[int, int]:
    numbers = [number for pl in kept if (number := pl.old_no if old else pl.new_no) is not None]
    count = len(numbers)
    if count == 0:
        # Convention: a zero-count range points at the line *before*.
        return max(fallback - 1, 0), 0
    return min(numbers), count


def _render(pl: PatchLine) -> str:
    if pl.kind is LineKind.CONTEXT:
        return f" {pl.content}"
    if pl.kind is LineKind.ADDITION:
        return f"+{pl.content}"
    if pl.kind is LineKind.DELETION:
        return f"-{pl.content}"
    return pl.content  # META passthrough
