"""Tag listing via ``git for-each-ref refs/tags``."""

from __future__ import annotations

from dataclasses import dataclass

SEP = "\x1f"
# %(*objectname) is the dereferenced commit for annotated tags, empty for
# lightweight ones — preferring it means "checkout tag" always lands on a commit.
FORMAT = (
    f"%(refname:short){SEP}%(objectname){SEP}%(*objectname){SEP}"
    f"%(contents:subject){SEP}%(creatordate:short)"
)


@dataclass
class Tag:
    name: str
    sha: str  # commit the tag points at (dereferenced when annotated)
    annotated: bool = False
    subject: str = ""
    when: str = ""


def parse_tags(data: str) -> list[Tag]:
    tags: list[Tag] = []
    for line in data.splitlines():
        if not line.strip():
            continue
        fields = (line.split(SEP) + [""] * 5)[:5]
        name, obj, deref, subject, when = fields
        tags.append(
            Tag(
                name=name,
                sha=deref or obj,
                annotated=bool(deref),
                subject=subject,
                when=when,
            )
        )
    # Newest first when dates are comparable, otherwise stable.
    return tags
