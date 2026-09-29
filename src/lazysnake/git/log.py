"""Parser for structured commit-log output.

We ask git for unit-separated fields::

    git log --pretty=format:%H%x1f%h%x1f%an%x1f%at%x1f%s%x1f%D%x1e

Records are terminated with ``%x1e`` so subjects may contain anything.
``parse_log_graph`` additionally handles ``--graph`` output, keeping the
ASCII lane prefix git renders for each commit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

SEP = "\x1f"  # field separator
REC = "\x1e"  # record terminator

FORMAT = f"%H{SEP}%h{SEP}%an{SEP}%at{SEP}%s{SEP}%D{SEP}{REC}"
# With --graph the lane prefix precedes the record on the same line; the
# leading \x1f marker lets us split prefix from fields.
GRAPH_FORMAT = f"%x1f{FORMAT}"


@dataclass
class Commit:
    sha: str
    short_sha: str
    author: str
    timestamp: int
    subject: str
    refs: str  # decorating refs, e.g. "HEAD -> main, origin/main"

    @property
    def when(self) -> str:
        """Human-ish local time, e.g. ``2026-09-29 14:03``."""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.timestamp))

    @property
    def branch_points(self) -> list[str]:
        """Ref names (without 'HEAD -> ') for display."""
        return [r.strip() for r in self.refs.split(",") if r.strip()]


@dataclass
class GraphCommit:
    """A commit plus the ``--graph`` ASCII lanes git drew around it."""

    commit: Commit
    lanes: str = ""  # may contain newlines for merge fan-outs


def _parse_record(record: str) -> Commit | None:
    record = record.strip("\n" + REC)
    if not record.strip():
        return None
    fields = record.split(SEP)
    while len(fields) < 6:
        fields.append("")
    sha, short, author, ts, subject, refs = fields[:6]
    if not sha:
        return None
    return Commit(
        sha=sha,
        short_sha=short,
        author=author,
        timestamp=int(ts or 0),
        subject=subject,
        refs=refs,
    )


def parse_log(data: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in data.split(REC):
        commit = _parse_record(record)
        if commit:
            commits.append(commit)
    return commits


def parse_log_graph(data: str) -> list[GraphCommit]:
    """Parse ``--graph --pretty=format:`` output into lane-decorated commits.

    Git emits lane continuation lines (``|``, ``|\\`` …) between commits and
    prefixes each record line with its own lane glyphs; both are collected
    into ``lanes``.
    """
    out: list[GraphCommit] = []
    pending = ""
    # NOTE: str.splitlines() would also split on \x1e (our record marker),
    # so split on "\n" explicitly.
    for line in data.split("\n"):
        if SEP in line:
            prefix, record = line.split(SEP, 1)
            commit = _parse_record(record)
            if commit:
                out.append(GraphCommit(commit=commit, lanes=pending + prefix))
            pending = ""
        else:
            pending += line + "\n"
    return out
