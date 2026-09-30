# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-29

Initial release: a [lazygit](https://github.com/jesseduffield/lazygit)-style
terminal UI for git, written in Python on top of
[Textual](https://github.com/Textualize/textual).

### Added

- **Files panel**: stage/unstage a file or all files, discard changes (with
  confirmation), ignore via `.gitignore`, copy path to clipboard, open in
  `$EDITOR`, substring filter, enter submodules.
- **Interactive diff**: colored rendering with line numbers, hunk-level and
  line-level staging, adjustable context size, horizontal scrolling for wide
  lines.
- **Committing**: commit, amend with pre-filled message, fixup commits.
- **History**: cherry-pick, revert, reword, drop and squash via rebase, move
  commits up/down, detached checkout, full rebase-todo editor, commit-graph
  lanes, apply a commit's patch to the worktree.
- **Patch mode**: mark a commit range (survives branch switches), apply it to
  the worktree or cherry-pick it onto any branch.
- **Undo/redo**: full-state snapshots (HEAD, index, worktree, untracked files,
  stash list) before every mutating action.
- **Branches & tags**: checkout, create, delete (incl. force and remote
  tracking), merge, fast-forward, rebase onto, set upstream; tag create,
  delete, checkout.
- **Stash**: push (with optional message), apply, pop, drop.
- **Remotes**: fetch, pull, push with live streamed output in the command log.
- **Conflicts**: resolve with ours/theirs/union; sequencer continue
  auto-skips empty picks.
- **Bisect**: mark bad/good, reset, status flag.
- **Worktrees & submodules**: list, create, remove, switch worktrees;
  submodule status, update --init, add, deinit.
- **Grep**: fixed-string search across tracked files with jump-to-match.
- **Global**: refresh with auto-refresh polling, command log pane, copy commit
  sha, file and commit filters, custom shell commands, full key remapping,
  recent-repositories switcher.
- Configuration via `~/.config/lazysnake/config.toml` (layout, key bindings,
  custom commands).

[0.1.0]: https://github.com/vkeerthivikram/lazysnake/releases/tag/v0.1.0
