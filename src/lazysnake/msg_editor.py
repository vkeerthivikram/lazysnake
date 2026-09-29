"""``GIT_EDITOR`` hook for rewords: overwrites the message file git opens.

Invoked by git as::

    python -m lazysnake.msg_editor <message-file> <COMMIT_EDITMSG>

Git opens the editor on COMMIT_EDITMSG; we replace its contents with the
planned message so the rebase continues non-interactively.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m lazysnake.msg_editor MESSAGE TARGET", file=sys.stderr)
        return 2
    message = Path(argv[1]).read_text()
    Path(argv[2]).write_text(message if message.endswith("\n") else message + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
