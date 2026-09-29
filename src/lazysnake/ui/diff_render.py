"""Render parsed diffs as styled rich Text for the main view."""

from __future__ import annotations

from rich.text import Text

from lazysnake.git.branch import Branch
from lazysnake.git.diff import FileDiff, Hunk, LineKind
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
    out = Text()
    for i, fd in enumerate(fds):
        if i:
            out.append("\n")
        out.append(render_file_diff(fd, note=note if i == 0 else None))
    return out


def render_untracked(path: str, content: str) -> Text:
    out = Text()
    out.append(f"new file: {path}\n", style=FILE_HEADER)
    out.append(f"@@ -0,0 +1,{len(content.splitlines())} @@\n", style=HEADER)
    for no, line in enumerate(content.splitlines(), start=1):
        out.append(f" {'' :4} {num_column(no)} ", style="dim")
        out.append("+" + line + "\n", style=ADD)
    return out


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
