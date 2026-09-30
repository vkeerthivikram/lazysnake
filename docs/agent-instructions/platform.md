# Platform Rules

## Overview

There are exactly three branch points in the codebase; keep it that way.

## Rules

### Tree-kill

- POSIX: `killpg` on the session leader. Windows: `taskkill /T /F`. Always through `_kill_process_group()` in `runner.py`. Never pass `start_new_session` on Windows.

### Submodule file protocol

- Local submodule URLs: `submodule_add` detects existing dirs, drive letters, and UNC paths to set `protocol.file.allow=always`.

### Paths and bytes

- Compare git output with `Path.as_posix()`. Fixture file writes must use `newline="\n"`, or Windows text mode stores CRLF and patches stop applying.
