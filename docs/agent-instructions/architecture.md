# Architecture Guidelines

## Overview

How lazysnake is wired. None of this is obvious from filenames.

## Rules

### git CLI layer

- `src/lazysnake/git/` is the git CLI layer. `runner.py` spawns every git subprocess and owns process-group kill, timeout, cancellation reap, bounded output, and index.lock retry. `snapshot.py` is the undo engine: fail-closed capture, symbolic-HEAD restore, conflict-index round-trip. The rest are parsers.

### UI composition

- `src/lazysnake/ui/app.py` is a roughly 500-line composition root. All actions live in `src/lazysnake/ui/actions/*.py` mixins composed onto `LazysnakeApp`. Panels call `self.app.<method>`; keep that interface stable.

### Custom commands

- Custom user commands in `ui/app.py` intentionally use `create_subprocess_shell` and their own kill path. Everything git-related goes through `git/runner.py`.
