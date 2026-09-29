"""Command-line entry point for lazysnake."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from lazysnake import __version__
from lazysnake.config import load_config
from lazysnake.git.runner import Git, GitError
from lazysnake.keymap import Keymap, bind_keys
from lazysnake.recent import record_recent
from lazysnake.ui.app import LazysnakeApp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lazysnake",
        description="A lazygit-style TUI for git, in Python.",
    )
    parser.add_argument("path", nargs="?", default=".", type=Path, help="repository path")
    parser.add_argument("--version", action="version", version=f"lazysnake {__version__}")
    args = parser.parse_args(argv)

    try:
        git = asyncio.run(Git.discover(args.path))
    except GitError:
        print(f"lazysnake: not a git repository: {args.path.resolve()}", file=sys.stderr)
        return 1

    config = load_config()
    keymap = Keymap.with_overrides(config.key_overrides)
    # Global keys are class-level; rebuild the app class with the user map.
    app_cls = bind_keys(LazysnakeApp, "global", keymap)
    record_recent(git.repo_root)
    app_cls(git, config=config, keymap=keymap).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
