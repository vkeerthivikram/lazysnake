# Testing Guidelines

## Overview

Tests run git for real in throwaway repos, plus headless Textual Pilot UI tests. CI runners are much slower than any dev machine, and most of these rules exist because of that gap.

## Rules

### Fixtures

- Real throwaway git repos through `RepoHelper` in `tests/conftest.py`, a bare-origin `remote_repo` fixture, submodule and worktree fixtures. Do not mock git output for parser tests.

### Timing

- **Never pause-and-assert app state.** CI runners are 3 to 4 times slower than a dev machine. Use the `eventually` fixture, a bounded wait, after keypresses and workers. A local green run proves nothing about slow-runner behavior.
- `RepoHelper.git` already retries on `index.lock` because the live app polls `git status` during tests.

### Regression coverage

- `test_poll_storm_never_blocks_or_corrupts_actions` is the regression test for the concurrency invariants. Keep it fast and green.

### Fakes

- Fake subprocesses: monkeypatch `lazysnake.git.runner.asyncio.create_subprocess_exec`. See `_fake_spawn` and `_ImmediateProc` in `tests/test_runner.py`.

### CI

- CI runs on every push over 8 jobs: ubuntu 3.11 to 3.14, macos 3.14, windows 3.14, lint, and build. Steps run under bash. First Windows or macOS failures after a change usually mean a timing or platform-assumption bug, not a flaky runner.
