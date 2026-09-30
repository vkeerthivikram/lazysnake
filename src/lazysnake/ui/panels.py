"""Left-column context panels, the main diff view, and the command log."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, TypeVar

from rich.text import Text
from textual.widgets import Label, ListItem, ListView, RichLog, Static

from lazysnake.git.branch import Branch
from lazysnake.git.diff import FileDiff, LineKind, PatchLine
from lazysnake.git.log import GraphCommit
from lazysnake.git.models import FileEntry, RepoSnapshot
from lazysnake.git.stash import StashEntry
from lazysnake.git.tags import Tag
from lazysnake.keymap import Keymap, bind
from lazysnake.ui.diff_render import num_column

if TYPE_CHECKING:
    from lazysnake.ui.app import LazysnakeApp

DEFAULT_KEYMAP = Keymap.defaults()

STAGED_STYLE = "rgb(63,185,80)"  # green
UNSTAGED_STYLE = "rgb(227,179,65)"  # yellow
UNTRACKED_STYLE = "rgb(248,113,113)"  # red
HEADER_STYLE = "dim bold"
CONFLICT_STYLE = "bold red"

PANEL_BORDER = "round $panel"
PANEL_BORDER_FOCUS = "round $accent"
MAX_DIFF_ROWS = 2000


class FileItem(ListItem):
    """A file row; ``staged_view`` says which side of the index it represents."""

    def __init__(self, entry: FileEntry, *, staged_view: bool) -> None:
        self.entry = entry
        self.staged_view = staged_view
        code = entry.display if not staged_view else f"{entry.display[0]} "
        style = (
            STAGED_STYLE
            if staged_view
            else (UNTRACKED_STYLE if entry.untracked else UNSTAGED_STYLE)
        )
        if entry.unmerged:
            style = CONFLICT_STYLE
        name = entry.path if entry.orig_path is None else f"{entry.orig_path} → {entry.path}"
        text = Text.assemble((f"{code:<2}", style), (" ", ""), (name, style))
        super().__init__(Label(text))

    @property
    def key(self) -> tuple[str, bool]:
        return (self.entry.path, self.staged_view)


@bind("files", DEFAULT_KEYMAP)
class FilesPanel(ListView):
    """Panel 1: staged and unstaged files, lazygit style."""

    if TYPE_CHECKING:
        # Panels dispatch to the composed app's action methods; narrow the
        # widget-level `.app` property to that class for the checker.
        app: LazysnakeApp

    ACTIONS: ClassVar[list[tuple[str, str]]] = [
        ("toggle_stage", "stage/unstage"),
        ("toggle_stage_all", "stage all"),
        ("commit", "commit"),
        ("amend", "amend"),
        ("discard", "discard"),
        ("stash", "stash"),
        ("stash_message", "stash w/ message"),
        ("ignore", "ignore"),
        ("copy_path", "copy path"),
        ("open_editor", "open in editor"),
        ("resolve_ours", "keep ours"),
        ("resolve_theirs", "keep theirs"),
        ("resolve_union", "keep both"),
        ("enter_entry", "enter submodule"),
        ("filter", "filter"),
    ]

    def __init__(self) -> None:
        super().__init__(id="files-panel")
        self.border_title = "1 Files"

    @property
    def selected_item(self) -> FileItem | None:
        return _selected(self, FileItem)

    def set_files(self, files: list[FileEntry], *, filter_text: str | None = None) -> None:
        previous = self.selected_item.key if self.selected_item else None
        self.clear()
        needle = (filter_text or "").lower()
        if needle:
            files = [f for f in files if needle in f.path.lower()]
        self.border_title = "1 Files" + (f"  /{filter_text}/" if needle else "")

        staged = [f for f in files if f.staged]
        unstaged = [f for f in files if f.unstaged and not f.staged]
        conflicts = [f for f in files if f.unmerged]

        rows: list[ListItem] = []
        if conflicts:
            rows.append(_header("Conflicts"))
            rows.extend(FileItem(f, staged_view=False) for f in conflicts)
        if staged:
            rows.append(_header("Staged changes"))
            rows.extend(FileItem(f, staged_view=True) for f in staged)
        if unstaged:
            rows.append(_header("Unstaged changes"))
            rows.extend(FileItem(f, staged_view=False) for f in unstaged)
        if not rows:
            rows.append(_header("(no matches)" if needle else "(no changes)"))

        for row in rows:
            self.append(row)

        restore = None
        for i, row in enumerate(rows):
            if isinstance(row, FileItem) and row.key == previous:
                restore = i
                break
        if restore is None:
            restore = next((i for i, r in enumerate(rows) if isinstance(r, FileItem)), None)
        self.index = restore

    def action_toggle_stage(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.toggle_stage(item.entry, staged_view=item.staged_view)

    def action_toggle_stage_all(self) -> None:
        self.app.toggle_stage_all()

    def action_commit(self) -> None:
        self.app.request_commit()

    def action_amend(self) -> None:
        self.app.request_amend()

    def action_discard(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_discard(item.entry)

    def action_stash(self) -> None:
        self.app.stash_push()

    def action_resolve_ours(self) -> None:
        item = self.selected_item
        if item is not None and item.entry.unmerged:
            self.app.resolve_conflict(item.entry, "ours")

    def action_resolve_theirs(self) -> None:
        item = self.selected_item
        if item is not None and item.entry.unmerged:
            self.app.resolve_conflict(item.entry, "theirs")

    def action_ignore(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.ignore_file(item.entry)

    def action_copy_path(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.copy_to_clipboard(item.entry.path)
            self.app.notify(f"Copied path: {item.entry.path}")

    def action_open_editor(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.open_in_editor(item.entry.path)

    def action_stash_message(self) -> None:
        self.app.request_stash_with_message()

    def action_resolve_union(self) -> None:
        item = self.selected_item
        if item is not None and item.entry.unmerged:
            self.app.resolve_conflict_union(item.entry)

    def action_enter_entry(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.enter_if_submodule(item.entry)

    def action_filter(self) -> None:
        self.app.request_filter("files")


def _header(label: str) -> ListItem:
    item = ListItem(Label(Text(label, style=HEADER_STYLE)))
    item.disabled = True
    return item


_SelectedT = TypeVar("_SelectedT", bound=ListItem)


def _selected(view: ListView, item_type: type[_SelectedT]) -> _SelectedT | None:
    """Return the highlighted child of ``view`` if it is an ``item_type``."""
    idx = view.index
    if idx is None:
        return None
    children = list(view.children)
    if 0 <= idx < len(children):
        child = children[idx]
        if isinstance(child, item_type):
            return child
    return None


def _restore_or_first(
    view: ListView, rows: Sequence[ListItem], previous: object, item_type: type[ListItem]
) -> None:
    restore = None
    for i, row in enumerate(rows):
        if isinstance(row, item_type) and getattr(row, "key", None) == previous:
            restore = i
            break
    if restore is None:
        restore = next((i for i, r in enumerate(rows) if isinstance(r, item_type)), None)
    view.index = restore


class BranchItem(ListItem):
    def __init__(self, branch: Branch) -> None:
        self.branch = branch
        self.key = branch.name
        if branch.is_remote:
            text = Text.assemble(("  ", ""), (branch.name, "magenta"))
        elif branch.is_head:
            text = Text.assemble(("* ", "bold green"), (branch.name, "bold cyan"))
        else:
            text = Text.assemble(("  ", ""), (branch.name, "cyan"))
        if branch.upstream:
            text.append(Text(f"  →{branch.upstream}", style="dim"))
        if branch.track:
            style = "red" if branch.track == "gone" else "yellow"
            text.append(Text(f" ({branch.track})", style=style))
        super().__init__(Label(text))


@bind("branches", DEFAULT_KEYMAP)
class BranchesPanel(ListView):
    """Panel 2: local and remote branches."""

    if TYPE_CHECKING:
        # Panels dispatch to the composed app's action methods; narrow the
        # widget-level `.app` property to that class for the checker.
        app: LazysnakeApp

    ACTIONS: ClassVar[list[tuple[str, str]]] = [
        ("checkout", "checkout"),
        ("new_branch", "new branch"),
        ("delete_branch", "delete"),
        ("force_delete_branch", "force delete"),
        ("merge_branch", "merge into current"),
        ("fast_forward", "fast-forward"),
        ("rebase_onto", "rebase onto"),
        ("set_upstream", "set upstream"),
    ]

    def __init__(self) -> None:
        super().__init__(id="branches-panel")
        self.border_title = "2 Branches"

    @property
    def selected_item(self) -> BranchItem | None:
        return _selected(self, BranchItem)

    def set_branches(self, local: list[Branch], remote: list[Branch]) -> None:
        previous = self.selected_item.key if self.selected_item else None
        self.clear()
        rows: list[ListItem] = []
        if local:
            rows.append(_header("Local"))
            rows.extend(BranchItem(b) for b in local)
        if remote:
            rows.append(_header("Remotes"))
            rows.extend(BranchItem(b) for b in remote)
        if not rows:
            rows.append(_header("(none)"))
        for row in rows:
            self.append(row)
        _restore_or_first(self, rows, previous, BranchItem)

    def action_checkout(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.checkout_branch(item.branch)

    def action_new_branch(self) -> None:
        self.app.request_create_branch()

    def action_delete_branch(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_delete_branch(item.branch)

    def action_merge_branch(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.merge_branch(item.branch)

    def action_force_delete_branch(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_delete_branch(item.branch, force=True)

    def action_fast_forward(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.fast_forward_branch(item.branch)

    def action_rebase_onto(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_rebase_onto(item.branch)

    def action_set_upstream(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_set_upstream(item.branch)


class CommitItem(ListItem):
    def __init__(self, graph: GraphCommit, *, filtered: bool = False) -> None:
        self.commit = graph.commit
        self.key = graph.commit.sha
        lanes = graph.lanes.rstrip("\n") if graph.lanes else ""
        text = Text()
        if lanes:
            text.append(Text(lanes + " ", style="dim cyan"))
        text.append(Text(f"{graph.commit.short_sha} ", "yellow"))
        if filtered:
            text.append(Text("·", "magenta"))
            text.append(Text(f" {graph.commit.subject}", ""))
        else:
            text.append(Text(graph.commit.subject, ""))
        if graph.commit.refs:
            text.append(Text(f"  {graph.commit.refs}", style="bold cyan"))
        text.append(Text(f"\n      {graph.commit.author}, {graph.commit.when}", style="dim"))
        super().__init__(Label(text))


@bind("commits", DEFAULT_KEYMAP)
class CommitsPanel(ListView):
    """Panel 3: commit log with git-rendered graph lanes."""

    if TYPE_CHECKING:
        # Panels dispatch to the composed app's action methods; narrow the
        # widget-level `.app` property to that class for the checker.
        app: LazysnakeApp

    ACTIONS: ClassVar[list[tuple[str, str]]] = [
        ("checkout_commit", "checkout (detached)"),
        ("cherry_pick", "cherry-pick"),
        ("revert", "revert"),
        ("reword", "reword"),
        ("fixup", "fixup commit"),
        ("drop_commit", "drop (rebase)"),
        ("squash_commit", "squash (rebase)"),
        ("move_up", "move up"),
        ("move_down", "move down"),
        ("copy_sha", "copy sha"),
        ("apply_patch", "apply patch"),
        ("bisect_bad", "bisect: bad"),
        ("bisect_good", "bisect: good"),
        ("bisect_reset", "bisect reset"),
        ("todo_editor", "edit todo from here"),
        ("filter", "filter"),
        ("patch_mark", "mark patch range"),
        ("patch_apply", "apply patch range"),
        ("patch_cherry", "cherry-pick range"),
        ("patch_clear", "clear patch"),
    ]

    def __init__(self) -> None:
        super().__init__(id="commits-panel")
        self.border_title = "3 Commits"
        self._base_title = "3 Commits"
        self._patch_suffix = ""

    @property
    def patch_suffix(self) -> str:
        return self._patch_suffix

    @patch_suffix.setter
    def patch_suffix(self, value: str) -> None:
        self._patch_suffix = value
        self.border_title = self._base_title + value

    @property
    def selected_item(self) -> CommitItem | None:
        return _selected(self, CommitItem)

    def set_commits(self, graph: list[GraphCommit], *, filter_text: str | None = None) -> None:
        previous = self.selected_item.key if self.selected_item else None
        self.clear()
        needle = (filter_text or "").lower()
        entries = (
            [g for g in graph if not needle or needle in g.commit.subject.lower()]
            if needle
            else graph
        )
        self._base_title = "3 Commits" + (f"  /{filter_text}/" if needle else "")
        self.border_title = self._base_title + self._patch_suffix
        if not entries:
            self.append(_header("(no matches)" if needle else "(no commits)"))
            self.index = None
            return
        rows = [CommitItem(g, filtered=bool(needle)) for g in entries]
        for row in rows:
            self.append(row)
        _restore_or_first(self, rows, previous, CommitItem)

    def action_cherry_pick(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.cherry_pick(item.commit)

    def action_revert(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_revert(item.commit)

    def action_drop_commit(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_drop_commit(item.commit)

    def action_squash_commit(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_squash_commit(item.commit)

    def action_checkout_commit(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_checkout_commit(item.commit)

    def action_reword(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_reword_commit(item.commit)

    def action_fixup(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.fixup_commit(item.commit)

    def action_move_up(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.move_commit(item.commit, "move-up")

    def action_move_down(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.move_commit(item.commit, "move-down")

    def action_copy_sha(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.copy_to_clipboard(item.commit.sha)
            self.app.notify(f"Copied {item.commit.short_sha}")

    def action_patch_mark(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.patch_mark(item.commit)

    def action_patch_apply(self) -> None:
        self.app.patch_apply()

    def action_patch_cherry(self) -> None:
        self.app.patch_cherry()

    def action_patch_clear(self) -> None:
        self.app.patch_clear()

    def action_apply_patch(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.apply_commit_patch(item.commit)

    def action_bisect_bad(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.bisect_mark(item.commit, "bad")

    def action_bisect_good(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.bisect_mark(item.commit, "good")

    def action_bisect_reset(self) -> None:
        self.app.request_bisect_reset()

    def action_todo_editor(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_todo_editor(item.commit)

    def action_filter(self) -> None:
        self.app.request_filter("commits")


class StashItem(ListItem):
    def __init__(self, entry: StashEntry) -> None:
        self.entry = entry
        self.key = entry.index
        text = Text.assemble((f"{entry.name}: ", "magenta"), (entry.subject, "yellow"))
        super().__init__(Label(text))


@bind("stash", DEFAULT_KEYMAP)
class StashPanel(ListView):
    """Panel 4: stash entries."""

    if TYPE_CHECKING:
        # Panels dispatch to the composed app's action methods; narrow the
        # widget-level `.app` property to that class for the checker.
        app: LazysnakeApp

    ACTIONS: ClassVar[list[tuple[str, str]]] = [
        ("apply_stash", "apply"),
        ("pop_stash", "pop"),
        ("drop_stash", "drop"),
    ]

    def __init__(self) -> None:
        super().__init__(id="stash-panel")
        self.border_title = "4 Stash"

    @property
    def selected_item(self) -> StashItem | None:
        return _selected(self, StashItem)

    def set_stash(self, entries: list[StashEntry]) -> None:
        previous = self.selected_item.key if self.selected_item else None
        self.clear()
        if not entries:
            self.append(_header("(empty)"))
            self.index = None
            return
        rows = [StashItem(e) for e in entries]
        for row in rows:
            self.append(row)
        _restore_or_first(self, rows, previous, StashItem)

    def action_apply_stash(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.apply_stash(item.entry)

    def action_pop_stash(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.pop_stash(item.entry)

    def action_drop_stash(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_drop_stash(item.entry)


class TagItem(ListItem):
    def __init__(self, tag: Tag) -> None:
        self.tag = tag
        self.key = tag.name
        text = Text.assemble((tag.name, "yellow"))
        if tag.when:
            text.append(Text(f"  {tag.when}", style="dim"))
        if tag.subject:
            text.append(Text(f"  {tag.subject}", style="dim"))
        super().__init__(Label(text))


@bind("tags", DEFAULT_KEYMAP)
class TagsPanel(ListView):
    """Panel 5: tags."""

    if TYPE_CHECKING:
        # Panels dispatch to the composed app's action methods; narrow the
        # widget-level `.app` property to that class for the checker.
        app: LazysnakeApp

    ACTIONS: ClassVar[list[tuple[str, str]]] = [
        ("checkout_tag", "checkout (detached)"),
        ("new_tag", "new tag"),
        ("delete_tag", "delete"),
    ]

    def __init__(self) -> None:
        super().__init__(id="tags-panel")
        self.border_title = "5 Tags"

    @property
    def selected_item(self) -> TagItem | None:
        return _selected(self, TagItem)

    def set_tags(self, tags: list[Tag]) -> None:
        previous = self.selected_item.key if self.selected_item else None
        self.clear()
        if not tags:
            self.append(_header("(none)"))
            self.index = None
            return
        rows = [TagItem(t) for t in tags]
        for row in rows:
            self.append(row)
        _restore_or_first(self, rows, previous, TagItem)

    def action_checkout_tag(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_checkout_tag(item.tag)

    def action_new_tag(self) -> None:
        self.app.request_create_tag()

    def action_delete_tag(self) -> None:
        item = self.selected_item
        if item is not None:
            self.app.request_delete_tag(item.tag)


class StatusBar(Static):
    """Branch/upstream summary at the top of the sidebar."""

    def __init__(self) -> None:
        super().__init__("", id="status-bar")
        self.border_title = "Status"

    def update_snapshot(self, snap: RepoSnapshot) -> None:
        branch = Text(snap.branch or "(unknown)", style="bold cyan")
        parts = [branch]
        if snap.upstream:
            parts.append(Text(f"  {snap.upstream}", style="dim"))
        if snap.ahead or snap.behind:
            parts.append(Text(f"  ↑{snap.ahead}", style="green"))
            parts.append(Text(f" ↓{snap.behind}", style="red"))
        if snap.in_conflict:
            parts.append(Text("  ⚠ merge conflict", style="bold red"))
        if snap.rebasing:
            parts.append(Text("  ⚑ REBASING", style="bold magenta"))
        if snap.merging:
            parts.append(Text("  ⚑ MERGING", style="bold magenta"))
        if snap.cherry_picking:
            parts.append(Text("  ⚑ CHERRY-PICK", style="bold magenta"))
        if snap.bisecting:
            parts.append(Text("  ⚑ BISECTING", style="bold blue"))
        staged = len(snap.staged_files)
        unstaged = len(snap.unstaged_files)
        parts.append(Text(f"\n{staged} staged · {unstaged} unstaged", style="dim"))
        self.update(Text.assemble(*parts))


class MainView(RichLog):
    """Right-hand pane: rendered text for non-diff views."""

    def __init__(self) -> None:
        super().__init__(id="main-view", markup=False, highlight=False, wrap=False)
        self.border_title = "Main"

    def show(self, renderable: Text) -> None:
        self.clear()
        self.write(renderable)


class DiffRow(ListItem):
    """One row of the interactive diff; carries hunk/line coordinates."""

    def __init__(
        self,
        text: Text,
        *,
        kind: LineKind | str,
        hunk_index: int,
        line_index: int | None = None,
    ) -> None:
        self.row_kind = kind
        self.hunk_index = hunk_index
        self.line_index = line_index
        super().__init__(Label(text))


@bind("diff", DEFAULT_KEYMAP)
class DiffView(ListView):
    """Interactive unified diff: space stages the hunk, l stages the line."""

    if TYPE_CHECKING:
        # Panels dispatch to the composed app's action methods; narrow the
        # widget-level `.app` property to that class for the checker.
        app: LazysnakeApp

    ACTIONS: ClassVar[list[tuple[str, str]]] = [
        ("stage_hunk", "stage hunk"),
        ("stage_line", "stage line"),
        ("scroll_left", "scroll left"),
        ("scroll_right", "scroll right"),
    ]

    def __init__(self) -> None:
        super().__init__(id="diff-view")
        self.border_title = "Diff"
        self.current_fd: FileDiff | None = None
        self.reverse = False  # True when viewing the staged side (space unstages)

    @property
    def selected_row(self) -> DiffRow | None:
        return _selected(self, DiffRow)

    def set_diff(self, fd: FileDiff, *, reverse: bool, note: str | None = None) -> None:
        self.current_fd = fd
        self.reverse = reverse
        self.clear()
        title = f"{fd.path}  (+{fd.additions} -{fd.deletions})"
        if reverse:
            title += "  [staged]"
        self.border_title = title
        expected_rows = (1 if note else 0) + sum(1 + len(hunk.lines) for hunk in fd.hunks)
        truncated = fd.truncated or expected_rows > MAX_DIFF_ROWS
        row_limit = MAX_DIFF_ROWS - (1 if truncated else 0)
        row_count = 0
        if note:
            self.append(_header(note))
            row_count += 1
        for hi, hunk in enumerate(fd.hunks):
            hunk_rows = 1 + len(hunk.lines)
            if row_count + hunk_rows > row_limit:
                truncated = True
                break
            self.append(
                DiffRow(Text(hunk.raw_header, style="bold cyan"), kind="hunk", hunk_index=hi)
            )
            row_count += 1
            for li, pl in enumerate(hunk.lines):
                self.append(
                    DiffRow(
                        _diff_line_text(pl),
                        kind=pl.kind,
                        hunk_index=hi,
                        line_index=li,
                    )
                )
            row_count += len(hunk.lines)
        if truncated:
            self.append(_header("Diff truncated; remaining hunks hidden"))

    def action_stage_hunk(self) -> None:
        self.app.stage_from_diff("hunk")

    def action_stage_line(self) -> None:
        self.app.stage_from_diff("line")

    def action_scroll_left(self) -> None:
        self.scroll_relative(x=-24, animate=False)

    def action_scroll_right(self) -> None:
        self.scroll_relative(x=24, animate=False)


def _diff_line_text(pl: PatchLine) -> Text:
    prefix = f" {num_column(pl.old_no)} {num_column(pl.new_no)} "
    if pl.kind is LineKind.ADDITION:
        return Text.assemble((prefix, "dim"), ("+" + pl.content, "rgb(63,185,80)"))
    if pl.kind is LineKind.DELETION:
        return Text.assemble((prefix, "dim"), ("-" + pl.content, "rgb(248,81,73)"))
    if pl.kind is LineKind.META:
        return Text("  " + pl.content, style="dim cyan")
    return Text.assemble((prefix, "dim"), (pl.content, ""))


class CommandLog(RichLog):
    """Bottom pane: every git command lazysnake runs on your behalf."""

    def __init__(self) -> None:
        super().__init__(id="command-log", markup=False, highlight=False, wrap=True, max_lines=500)
        self.border_title = "Command log"

    def write_command(self, command: str, *, ok: bool, detail: str = "") -> None:
        marker = Text("✓ " if ok else "✗ ", style="green" if ok else "red")
        line = Text.assemble(marker, (command, "" if ok else "bold red"))
        self.write(line)
        if detail:
            self.write(Text(detail, style="dim red"))

    def write_output(self, text: str, *, is_stderr: bool = False) -> None:
        """Live output line from a streaming command (remote push/pull etc.)."""
        self.write(Text(text, style="dim red" if is_stderr else "dim"))
