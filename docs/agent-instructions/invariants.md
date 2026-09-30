# Invariants

## Overview

Each of these broke CI for days.

## Rules

1. **One index writer.** git's index allows a single writer. `git status` refreshes its stat cache under `index.lock` and checkpoints `write-tree` under it too. Every index-touching path holds `app._git_lock`: `run_git`, `run_git_stream`, the refresh body, and undo/redo capture plus restore. New mutation paths must go through `run_git` or `mutate()`. Never call `self.git.*` mutating commands directly from UI code.

2. **Refresh lifecycle.** Call `refresh_state()`. It sets the in-flight flag synchronously and increments an owner token; only the newest worker clears the flag. Await refreshes through `self._refreshed()`, which treats supersession by a newer refresh as success. The poll timer skips ticks while a refresh is in flight. Do not add refresh callers that bypass the flag check.

3. **Action boilerplate.** Workers use `mutate()`, which applies the uniform error, notify, refresh, and checkpoint policy. Confirm modals use `self.confirm()`. Do not hand-roll `try/except GitError` plus refresh.

4. **Keymap exhaustiveness.** Every action id bound in `keymap.py` must exist as `action_<id>` or the explicit method name on its class. `tests/test_keymap.py` enforces this. Bindings are installed by the `@bind` class decorator, and class names are load-bearing for Textual CSS.

5. **Modal results are dataclasses** defined next to their screens in `modals.py`. No dict-dismiss contracts.

6. **Fail closed.** An incomplete undo snapshot blocks the mutation and logs `checkpoint (snapshot failed; action blocked)` with the GitError. Never make capture "best effort".

7. **Diff truncation.** Bounded diff reads drop incomplete trailing hunks, and staging on truncated hunks is disabled. Partial patches must never reach `git apply`.
