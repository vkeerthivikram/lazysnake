"""Parser and patch builder for unified diffs from ``git diff --no-color``.

The parser keeps enough raw material (``raw_header`` on hunks, header lines on
files) that selected hunks can be rebuilt into a patch ``git apply`` accepts.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field


class LineKind(enum.Enum):
    CONTEXT = "context"
    ADDITION = "addition"
    DELETION = "deletion"
    META = "meta"  # "\\ No newline at end of file"


@dataclass
class PatchLine:
    kind: LineKind
    content: str  # without the leading +/-/space marker
    old_no: int | None = None
    new_no: int | None = None


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    raw_header: str  # the @@ line, verbatim
    section: str = ""  # function/context name after the second @@
    lines: list[PatchLine] = field(default_factory=list)

    def to_patch(self) -> str:
        body = "\n".join(_render_line(pl) for pl in self.lines)
        return f"{self.raw_header}\n{body}"

    @property
    def additions(self) -> int:
        return sum(1 for pl in self.lines if pl.kind is LineKind.ADDITION)

    @property
    def deletions(self) -> int:
        return sum(1 for pl in self.lines if pl.kind is LineKind.DELETION)


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    raw_git_header: str = ""  # the "diff --git a/x b/x" line, verbatim
    header_lines: list[str] = field(default_factory=list)  # index/mode/rename lines
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    is_new: bool = False
    is_deleted: bool = False
    rename_from: str | None = None
    rename_to: str | None = None

    @property
    def path(self) -> str:
        """Display path: new name for renames, existing side otherwise."""
        if self.rename_to or self.rename_from:
            return self.rename_to or self.rename_from or ""
        return self.new_path or self.old_path

    @property
    def additions(self) -> int:
        return sum(h.additions for h in self.hunks)

    @property
    def deletions(self) -> int:
        return sum(h.deletions for h in self.hunks)

    def to_patch(self, hunks: list[Hunk] | None = None) -> str:
        """Rebuild an appliable patch, optionally with only some hunks."""
        chosen = self.hunks if hunks is None else hunks
        parts = [self.raw_git_header, *self.header_lines, *(h.to_patch() for h in chosen)]
        return "\n".join(p for p in parts if p) + "\n"


_HUNK_RE = re.compile(
    r"^@@ -(?P<os>\d+)(?:,(?P<oc>\d+))? \+(?P<ns>\d+)(?:,(?P<nc>\d+))? @@(?: (?P<section>.*))?$"
)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<a>.+) b/(?P<b>.+)$")


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a unified diff into per-file structures."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0

    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = _new_file_record(line)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue

        if line.startswith("@@ ") and (m := _HUNK_RE.match(line)):
            hunk = Hunk(
                old_start=int(m.group("os")),
                old_count=int(m.group("oc") or "1"),
                new_start=int(m.group("ns")),
                new_count=int(m.group("nc") or "1"),
                raw_header=line,
                section=m.group("section") or "",
            )
            cur.hunks.append(hunk)
            # For zero-count sides git reports the line *before* the range; the
            # first line of that kind then takes the following number.
            old_no = hunk.old_start + (1 if hunk.old_count == 0 else 0)
            new_no = hunk.new_start + (1 if hunk.new_count == 0 else 0)
            continue

        if hunk is None:
            _absorb_header_line(cur, line)
            continue

        if line.startswith("+"):
            hunk.lines.append(PatchLine(LineKind.ADDITION, line[1:], new_no=new_no))
            new_no += 1
        elif line.startswith("-"):
            hunk.lines.append(PatchLine(LineKind.DELETION, line[1:], old_no=old_no))
            old_no += 1
        elif line.startswith("\\"):
            hunk.lines.append(PatchLine(LineKind.META, line))
        else:
            # Context lines are " " + content; tolerate a bare empty line.
            content = line[1:] if line.startswith(" ") else line
            hunk.lines.append(
                PatchLine(LineKind.CONTEXT, content, old_no=old_no, new_no=new_no)
            )
            old_no += 1
            new_no += 1

    return files


def _new_file_record(git_line: str) -> FileDiff:
    m = _DIFF_GIT_RE.match(git_line)
    old_path = m.group("a") if m else git_line.removeprefix("diff --git ")
    new_path = m.group("b") if m else old_path
    return FileDiff(
        old_path=old_path,
        new_path=new_path,
        raw_git_header=git_line,
    )


def _absorb_header_line(fd: FileDiff, line: str) -> None:
    """File-level metadata between 'diff --git' and the first hunk.

    Every line is kept verbatim (patch reconstruction needs the exact
    header); interesting ones are also parsed into fields.
    """
    fd.header_lines.append(line)
    if line.startswith("new file mode "):
        fd.is_new = True
    elif line.startswith("deleted file mode "):
        fd.is_deleted = True
    elif line.startswith("rename from "):
        fd.rename_from = line.removeprefix("rename from ")
        fd.old_path = fd.rename_from
    elif line.startswith("rename to "):
        fd.rename_to = line.removeprefix("rename to ")
        fd.new_path = fd.rename_to
    elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
        fd.is_binary = True
        fd.header_lines.pop()  # git prints these instead of hunks, not part of an appliable patch
    elif line.startswith("--- "):
        # Authoritative old path (handles spaces the diff --git line cannot).
        p = line[4:]
        if p != "/dev/null":
            fd.old_path = p.removeprefix("a/")
    elif line.startswith("+++ "):
        p = line[4:]
        if p != "/dev/null":
            fd.new_path = p.removeprefix("b/")


def _render_line(pl: PatchLine) -> str:
    if pl.kind is LineKind.CONTEXT:
        return f" {pl.content}"
    if pl.kind is LineKind.ADDITION:
        return f"+{pl.content}"
    if pl.kind is LineKind.DELETION:
        return f"-{pl.content}"
    return pl.content  # META lines pass through verbatim
