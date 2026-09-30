# AGENTS.md

lazysnake is a lazygit-style TUI for git, Python/Textual, one package under `src/lazysnake`.

## Commands

- Setup: `uv sync --locked`. `uv` is the only Python tool here; the lockfile is authoritative and `textual>=8.2` is the floor.
- Tests: `uv run pytest -q`. Takes about 80 seconds because it builds real git repos in tmp. Single test: `uv run pytest tests/test_fixes.py::test_name`. Use `uv run --no-sync` while another process might be syncing.
- Gates before "done": `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`. Mypy is strict and scoped to `src/`; tests are relaxed by design.
- Packaging check: `uv build --out-dir <tmp>`. The version lives in `src/lazysnake/__init__.py` through hatch dynamic versioning. Never set it in pyproject.toml.
- No commit or push without explicit user approval.

## Detailed instructions

- [Architecture](docs/agent-instructions/architecture.md)
- [Invariants](docs/agent-instructions/invariants.md)
- [Platform rules](docs/agent-instructions/platform.md)
- [Testing](docs/agent-instructions/testing.md)
- [Style](docs/agent-instructions/style.md)
