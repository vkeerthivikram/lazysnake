"""Render parsed diffs as styled rich Text for the main view."""

from __future__ import annotations

import io
from collections.abc import Sequence

from rich.text import Text

from lazysnake.git.branch import Branch
from lazysnake.git.diff import FileDiff, Hunk, LineKind, PatchLine
from lazysnake.git.log import Commit
from lazysnake.git.stash import StashEntry
from lazysnake.git.tags import Tag

ADD = "rgb(63,185,80)"  # green
DEL = "rgb(248,81,73)"  # red
CONTEXT = ""
META = "dim cyan"
HEADER = "bold cyan"
FILE_HEADER = "bold yellow"
NOTE = "dim italic"

# Rendered-line budget for the main view. One giant file must not build a
# million styled rows on the event loop (grep results cap at 200 rows for
# the same reason — see modals.GrepScreen).
MAX_RENDER_LINES = 2000


def _cap_note(more: int) -> str:
    unit = "line" if more == 1 else "lines"
    return f"... ({more} more {unit} not shown)"


def num_column(n: int | None, width: int = 4) -> str:
    """Right-aligned line-number column (blank for absent sides)."""
    if n is None:
        return " " * width
    return str(n).rjust(width)


def render_hunk(hunk: Hunk) -> Text:
    out = Text()
    out.append(hunk.raw_header + "\n", style=HEADER)
    for pl in hunk.lines:
        if pl.kind is LineKind.CONTEXT:
            out.append(f" {num_column(pl.old_no)} {num_column(pl.new_no)} ", style="dim")
            out.append(pl.content + "\n")
        elif pl.kind is LineKind.ADDITION:
            out.append(f" {num_column(pl.old_no)} {num_column(pl.new_no)} ", style="dim")
            out.append("+" + pl.content + "\n", style=ADD)
        elif pl.kind is LineKind.DELETION:
            out.append(f" {num_column(pl.old_no)} {num_column(pl.new_no)} ", style="dim")
            out.append("-" + pl.content + "\n", style=DEL)
        else:  # META: "\ No newline at end of file"
            out.append("  " + pl.content + "\n", style=META)
    return out


def render_file_diff(fd: FileDiff, *, note: str | None = None) -> Text:
    out = Text()
    change = fd.path
    if fd.rename_from:
        change = f"{fd.rename_from} → {fd.rename_to}"
    if fd.is_new:
        change = f"new file: {fd.path}"
    elif fd.is_deleted:
        change = f"deleted: {fd.path}"
    out.append(f"{change}  (+{fd.additions} -{fd.deletions})\n", style=FILE_HEADER)
    if note:
        out.append(note + "\n", style=NOTE)
    if fd.is_binary:
        out.append("  (binary file)\n", style=NOTE)
        return out
    for hunk in fd.hunks:
        out.append(render_hunk(hunk))
    return out


def render_file_diffs(fds: list[FileDiff], *, note: str | None = None) -> Text:
    """Render file diffs, capped at ``MAX_RENDER_LINES`` rendered lines.

    Beyond the cap a trailing "(N more lines)" note replaces the rest, so
    one enormous diff cannot freeze the view. Lines are appended directly;
    no whole-file or whole-hunk Rich ``Text`` is built first.
    """
    out = Text()
    total_rows = max(0, len(fds) - 1) + sum(
        1
        + (1 if i == 0 and note else 0)
        + (1 if fd.is_binary else sum(1 + len(hunk.lines) for hunk in fd.hunks))
        for i, fd in enumerate(fds)
    )
    source_truncated = any(fd.truncated for fd in fds)
    capped = source_truncated or total_rows > MAX_RENDER_LINES
    content_limit = MAX_RENDER_LINES - (1 if capped else 0)
    used = 0

    for i, fd in enumerate(fds):
        if i:
            if used >= content_limit:
                break
            out.append("\n")
            used += 1
        if used >= content_limit:
            break

        change = fd.path
        if fd.rename_from:
            change = f"{fd.rename_from} → {fd.rename_to}"
        if fd.is_new:
            change = f"new file: {fd.path}"
        elif fd.is_deleted:
            change = f"deleted: {fd.path}"
        out.append(f"{change}  (+{fd.additions} -{fd.deletions})\n", style=FILE_HEADER)
        used += 1
        if i == 0 and note and used < content_limit:
            out.append(note + "\n", style=NOTE)
            used += 1
        if fd.is_binary:
            if used < content_limit:
                out.append("  (binary file)\n", style=NOTE)
                used += 1
            continue

        for hunk in fd.hunks:
            if used >= content_limit:
                break
            out.append(hunk.raw_header + "\n", style=HEADER)
            used += 1
            for pl in hunk.lines:
                if used >= content_limit:
                    break
                _append_patch_line(out, pl)
                used += 1

    if capped:
        if source_truncated:
            out.append("... (diff truncated; additional hunks or files hidden)\n", style=NOTE)
        else:
            out.append(_cap_note(total_rows - used) + "\n", style=NOTE)
    return out


def render_untracked(
    path: str, content: str | Sequence[str], *, total_lines: int | None = None
) -> Text:
    """Render an untracked file, capped at ``MAX_RENDER_LINES`` lines."""
    out = Text()
    if isinstance(content, str):
        lines: Sequence[str] | io.StringIO = io.StringIO(content)
        total = (
            total_lines
            if total_lines is not None
            else (content.count("\n") + (1 if content and not content.endswith("\n") else 0))
        )
    else:
        lines = content
        total = total_lines if total_lines is not None else len(content)
    out.append(f"new file: {path}\n", style=FILE_HEADER)
    out.append(f"@@ -0,0 +1,{total} @@\n", style=HEADER)
    for no, line in enumerate(lines, start=1):
        if no > MAX_RENDER_LINES:
            break
        out.append(f" {'':4} {num_column(no)} ", style="dim")
        out.append("+" + line.rstrip("\r\n") + "\n", style=ADD)
    if total > MAX_RENDER_LINES:
        out.append(_cap_note(total - MAX_RENDER_LINES) + "\n", style=NOTE)
    return out


def _append_patch_line(out: Text, pl: PatchLine) -> None:
    if pl.kind is LineKind.CONTEXT:
        out.append(f" {num_column(pl.old_no)} {num_column(pl.new_no)} ", style="dim")
        out.append(pl.content + "\n")
    elif pl.kind is LineKind.ADDITION:
        out.append(f" {num_column(pl.old_no)} {num_column(pl.new_no)} ", style="dim")
        out.append("+" + pl.content + "\n", style=ADD)
    elif pl.kind is LineKind.DELETION:
        out.append(f" {num_column(pl.old_no)} {num_column(pl.new_no)} ", style="dim")
        out.append("-" + pl.content + "\n", style=DEL)
    else:
        out.append("  " + pl.content + "\n", style=META)


def render_message(message: str) -> Text:
    """Plain informational text for the main view."""
    return Text(message + "\n", style=NOTE)


def render_commit(commit: Commit, files: list[FileDiff]) -> Text:
    out = Text()
    out.append(f"commit {commit.sha}\n", style="bold yellow")
    out.append(f"Author: {commit.author}\n", style="")
    out.append(f"Date:   {commit.when}\n", style="")
    if commit.refs:
        out.append(f"Refs:   {commit.refs}\n", style="bold cyan")
    out.append("\n")
    out.append(f"    {commit.subject}\n\n")
    if files:
        out.append(render_file_diffs(files))
    else:
        out.append("    (no textual diff)\n", style=NOTE)
    return out


def render_branch_view(branch: Branch, commits: list[Commit]) -> Text:
    out = Text()
    marker = "* " if branch.is_head else "  "
    out.append(f"{marker}{branch.name}", style="bold cyan")
    if branch.upstream:
        out.append(f"  → {branch.upstream}", style="dim")
    if branch.track:
        out.append(f"  ({branch.track})", style="yellow")
    out.append(f"\n  {branch.sha[:12]}\n\n")
    if not commits:
        out.append("  (no commits)\n", style=NOTE)
    for c in commits:
        out.append(f"  {c.short_sha} ", style="yellow")
        out.append(f"{c.subject}\n")
        out.append(f"        {c.author}, {c.when}\n", style="dim")
    return out


def render_stash_view(entry: StashEntry, files: list[FileDiff]) -> Text:
    out = Text()
    out.append(f"{entry.name}\n", style="bold magenta")
    out.append(f"{entry.subject}\n", style="dim")
    out.append(f"{entry.when}\n\n", style="dim")
    if files:
        out.append(render_file_diffs(files))
    else:
        out.append("  (no textual diff)\n", style=NOTE)
    return out


def render_tag_view(tag: Tag, commits: list[Commit]) -> Text:
    out = Text()
    out.append(f"tag {tag.name}\n", style="bold yellow")
    out.append(f"commit {tag.sha}\n", style="dim")
    if tag.annotated:
        out.append("annotated tag\n", style="dim")
    if tag.subject:
        out.append(f"\n    {tag.subject}\n", style="")
    out.append("\n")
    if not commits:
        out.append("  (no commits)\n", style=NOTE)
    for c in commits:
        out.append(f"  {c.short_sha} ", style="yellow")
        out.append(f"{c.subject}\n")
        out.append(f"        {c.author}, {c.when}\n", style="dim")
    return out
