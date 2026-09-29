"""Local-branch listing via ``git for-each-ref refs/heads``."""

from __future__ import annotations

from dataclasses import dataclass

SEP = "\x1f"
FORMAT = (
    f"%(refname:short){SEP}%(objectname){SEP}%(HEAD){SEP}"
    f"%(upstream:short){SEP}%(upstream:track)"
)


@dataclass
class Branch:
    name: str
    sha: str
    is_head: bool = False
    upstream: str = ""
    track: str = ""  # "ahead 2", "behind 1", "gone", or ""
    is_remote: bool = False  # refs/remotes entry, e.g. origin/main

    @property
    def short_name(self) -> str:
        """Local part for remote branches (``origin/feat`` → ``feat``)."""
        return self.name.split("/", 1)[1] if self.is_remote and "/" in self.name else self.name


def parse_branches(data: str, *, remote: bool = False) -> list[Branch]:
    branches: list[Branch] = []
    for line in data.splitlines():
        if not line.strip():
            continue
        fields = (line.split(SEP) + [""] * 5)[:5]
        name, sha, head, upstream, track = fields
        if remote and name.endswith("/HEAD"):
            continue  # symbolic ref, not a real branch
        branches.append(
            Branch(
                name=name,
                sha=sha,
                is_head=head == "*",
                upstream=upstream,
                track=track.strip("[ ]"),
                is_remote=remote,
            )
        )
    # HEAD first, then alphabetical.
    branches.sort(key=lambda b: (not b.is_head, b.name))
    return branches
