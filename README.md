# lazysnake

A [lazygit](https://github.com/jesseduffield/lazygit)-style terminal UI for git,
written in Python on top of [Textual](https://github.com/Textualize/textual).

Left column: Status / Files / Branches (local + remote) / Commits (with graph
lanes) / Stash / Tags. Right: interactive diff (stage hunks or single lines,
scroll wide lines). Bottom: command log with live streamed output from remote
operations.

## Feature matrix vs lazygit

Legend: ✅ done & tested · ⚠️ partial

| Area | Feature | State |
| ---- | ------- | ----- |
| Files | stage/unstage file, stage all | ✅ |
| Files | discard changes (confirm) | ✅ |
| Files | ignore file (.gitignore) | ✅ |
| Files | copy path to clipboard | ✅ |
| Files | open in `$EDITOR` | ✅ |
| Files | filter by substring (`/`) | ✅ |
| Files | enter submodule | ✅ |
| Diff | colored diff, line numbers | ✅ |
| Diff | hunk-level staging | ✅ |
| Diff | line-level staging | ✅ |
| Diff | context size (`[` `]`) | ✅ |
| Diff | horizontal scroll for wide lines (`H` `L`) | ✅ |
| Commit | commit, amend (pre-filled) | ✅ |
| Commit | fixup commit | ✅ |
| History | cherry-pick, revert | ✅ |
| History | reword commit | ✅ |
| History | drop / squash via rebase | ✅ |
| History | move commit up / down | ✅ |
| History | checkout commit (detached) | ✅ |
| History | full rebase-todo editor (`T`) | ✅ |
| History | commit-graph lanes | ✅ |
| History | apply commit patch to worktree (`w`) | ✅ |
| History | patch mode: mark range, apply/cherry-pick onto any branch (`m`/`a`/`c`) | ✅ |
| History | undo / redo of lazysnake actions (`z` / `ctrl+y`) | ✅ full-state snapshots: HEAD, index, worktree, untracked files, stash list |
| Branches | checkout / create / delete / force delete | ✅ |
| Branches | merge, fast-forward, rebase onto | ✅ |
| Branches | set upstream | ✅ |
| Branches | remote branches (browse, checkout --track, delete) | ✅ |
| Tags | create / delete / checkout | ✅ |
| Stash | push (incl. message), apply, pop, drop | ✅ |
| Remote | fetch / pull / push with live streamed output | ✅ |
| Conflicts | ours / theirs / union (`o` / `t` / `b`) | ✅ |
| Conflicts | sequencer continue handles empty picks (auto-skip) | ✅ |
| Bisect | mark bad/good, reset, status flag | ✅ |
| Worktrees | list / create / remove / switch (`W`) | ✅ |
| Submodules | status, enter, update --init, add, deinit (`U`) | ✅ |
| Grep | fixed-string search, jump to match, land on hunk, stage file (`ctrl+g`) | ✅ |
| Global | refresh, auto-refresh poll | ✅ |
| Global | command log pane with streamed remote output | ✅ |
| Global | copy commit sha | ✅ |
| Global | commits filter (`/`) | ✅ |
| Global | custom shell commands (config `[[custom]]`) | ✅ |
| Global | recent-repos switcher (`ctrl+r`) | ✅ |
| Global | remap any binding via config `[keys]` | ✅ |
| — | staged-file grep with per-line staging from matches | ⚠️ grep jumps to the hunk; staging uses the normal diff view |
| — | bisect visualisation, interactive patch editing | ❌ |

## Install & run

```sh
uv sync                 # create venv and install dependencies
uv run lazysnake        # run inside any git repository
uv run pytest           # run the test suite (125 tests)
uv run ruff check .     # lint
```

Or point it at a repo explicitly: `uv run lazysnake ~/code/myproject`.

## Configuration

Optional TOML at `~/.config/lazysnake/config.toml`:

```toml
sidebar_width = 46   # left column width in cells (min 20)
poll_seconds  = 2.0  # background status refresh interval (0.5–60)
log_limit     = 500  # commits fetched for the log panel

# Rebind anything: section = panel, key = action. Unknown ids are reported
# at startup; duplicates within a panel are errors.
[keys.files]
commit = "K"
amend = "alt+A"

[keys.global]
undo = "ctrl+z"

# Custom commands: keys fire when nothing else is bound.
[[custom]]
key = "!"
command = "git status -sb"

[[custom]]
key = "X"
command = "git gc"
confirm = true
```

Recent repositories are remembered in `~/.config/lazysnake/repos.json`.

## Keybindings

Defaults below; every one is remappable via `[keys]`.

### Global

| Key | Action |
| --- | ------ |
| `1`–`5` | focus Files / Branches / Commits / Stash / Tags |
| `6` | focus the interactive diff |
| `f` / `p` / `P` | fetch / pull / push (live output in command log) |
| `R` | continue whichever sequencer is active: cherry-pick, merge, or rebase (skips empty picks) |
| `K` | abort rebase |
| `z` / `ctrl+y` | undo / redo last lazysnake action — restores HEAD, index, worktree, untracked files and stashes exactly |
| `[` / `]` | less / more diff context |
| `ctrl+g` | grep tracked files |
| `ctrl+r` | recent repositories |
| `W` | worktrees · `U` submodules |
| `r` | refresh · `q` quit |

### Files panel

| Key | Action |
| --- | ------ |
| `space` | stage / unstage selected file |
| `a` | stage all / unstage all |
| `c` / `A` | commit / amend (editor pre-filled) |
| `d` | discard changes (confirm) |
| `s` / `S` | stash / stash with message |
| `i` | add to .gitignore |
| `y` | copy path · `E` open in `$EDITOR` |
| `o` / `t` / `b` | conflicted file: keep ours / theirs / both (union) |
| `enter` | enter submodule directory |
| `/` | filter files by substring (empty clears) |

### Diff view (press `6`)

| Key | Action |
| --- | ------ |
| `space` | stage (or unstage, on the staged side) the hunk under the cursor |
| `l` | stage just the selected `+`/`-` line |
| `H` / `L` | scroll left / right for wide lines |

### Branches panel (local + remote)

| Key | Action |
| --- | ------ |
| `space` | checkout (remote: creates tracking branch) |
| `n` | new branch · `d` delete · `D` force delete |
| `M` / `f` / `r` | merge / fast-forward / rebase onto |
| `u` | set upstream to `origin/<branch>` |

### Commits panel

| Key | Action |
| --- | ------ |
| `space` | checkout commit (detached, confirm) |
| `C` / `v` | cherry-pick / revert (confirm) |
| `r` | reword · `f` fixup |
| `D` / `S` | drop / squash-into-parent (confirm) |
| `<` / `>` | move commit up / down |
| `w` | apply commit's patch to the worktree |
| `T` | full rebase-todo editor from this commit |
| `m` | patch mode: first mark = anchor, second mark = range end (survives branch switches) |
| `a` / `c` | apply marked range to worktree / cherry-pick marked range onto current branch |
| `x` | clear patch |
| `B` / `G` / `ctrl+b` | bisect: mark bad / good / reset |
| `Y` | copy sha · `/` filter by subject |

### Tags panel

| Key | Action |
| --- | ------ |
| `space` checkout · `n` new · `d` delete (confirm) |

### Stash panel

| Key | Action |
| --- | ------ |
| `space` apply · `p` pop · `d` drop (confirm) |

### Submodules screen (`U`)

| Key | Action |
| --- | ------ |
| `enter` open · `u` update --init --recursive · `a` add · `d` deinit (confirm) |

## Architecture

```
src/lazysnake/
├── cli.py              entry point (repo discovery, keymap build, recent repos)
├── config.py           config.toml: sizes, custom commands, [keys] overrides
├── keymap.py           data-driven bindings: DEFAULT_KEYS + Keymap + bind_keys
├── recent.py           recent-repositories registry
├── git/                async git runner (+ run_streaming) and parsers:
│                       status, diff, log+graph, branch, stash, tags,
│                       worktree, submodule, merge/union, snapshot (undo)
├── staging.py          hunk/line patch forging for `git apply --cached`
├── rebase.py           TodoPlan + GIT_SEQUENCE_EDITOR rebase planning
├── sequence_editor.py  the todo-rewriting hook git invokes
├── msg_editor.py       the message-writing hook for rewords
└── ui/                 Textual app: panels, diff view, modals (commit,
                        confirm, input, todo editor, recent, worktrees,
                        submodules, grep)
```

Git is driven exactly the way lazygit drives it: subprocess calls with
machine-readable formats (`--porcelain=v2 -z`, unit-separated log fields).
No pygit2, no compiled dependencies.

Undo works the way lazygit's does: before every mutating action lazysnake
captures a full snapshot (HEAD, index tree, worktree content including
untracked files via a temporary index, and the stash list), anchored in a
commit chain on `refs/lazysnake/snapshots`; undo/redo restore that state
exactly.

## Testing

```sh
uv run pytest          # 125 tests: parser fixtures, real temp repos (incl.
                       # a bare origin, a real submodule, and worktrees),
                       # and headless Textual pilots driving the actual UI
```
