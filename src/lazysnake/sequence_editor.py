"""``GIT_SEQUENCE_EDITOR`` hook: rewrites the rebase todo per a JSON plan.

Invoked by git as::

    python -m lazysnake.sequence_editor <plan.json> <git-rebase-todo>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lazysnake.rebase import rewrite_todo


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m lazysnake.sequence_editor PLAN TODO", file=sys.stderr)
        return 2
    plan = json.loads(Path(argv[1]).read_text())
    todo = Path(argv[2])
    todo.write_text(rewrite_todo(todo.read_text(), plan.get("ops", {}), order=plan.get("order")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
