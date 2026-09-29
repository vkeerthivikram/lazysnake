"""Submodule listing and state via ``git submodule status``."""

from __future__ import annotations

from dataclasses import dataclass

from lazysnake.git.runner import Git


@dataclass
class Submodule:
    path: str
    sha: str = ""
    flag: str = " "  # ' ' ok, '+' checked-out sha differs, '-' not initialized, 'U' merge conflicts

    @property
    def state(self) -> str:
        if self.flag == "-":
            return "not initialized"
        if self.flag == "+":
            return "checked-out sha differs"
        if self.flag == "U":
            return "merge conflicts"
        return "ok"


async def submodules(git: Git) -> list[Submodule]:
    """All submodules with state; empty when there are none."""
    try:
        data = await git.run("submodule", "status")
    except Exception:
        return []
    out: list[Submodule] = []
    for line in data.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        flag = line[:1] if line[:1] in " +-U" else " "
        parts = stripped.lstrip(" +-U").split(" ", 1)
        if len(parts) != 2:
            continue
        sha, rest = parts
        out.append(Submodule(path=rest.split(" (")[0].strip(), sha=sha, flag=flag))
    return out


async def submodule_paths(git: Git) -> list[str]:
    """Back-compat helper: just the paths."""
    return [s.path for s in await submodules(git)]
