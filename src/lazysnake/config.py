"""User configuration loaded from ``~/.config/lazysnake/config.toml``.

Values are deliberately few; anything not set falls back to defaults, and
a malformed config never stops the app from starting.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

CONFIG_PATH = Path.home() / ".config" / "lazysnake" / "config.toml"


@dataclass(frozen=True)
class CustomCommand:
    """A user-defined shell command bound to a key (config ``[[custom]]``)."""

    key: str
    command: str
    description: str = ""
    confirm: bool = False


@dataclass(frozen=True)
class Config:
    sidebar_width: int = 46
    poll_seconds: float = 2.0
    log_limit: int = 500
    custom_commands: tuple[CustomCommand, ...] = ()
    key_overrides: dict[str, dict[str, str]] = field(default_factory=dict)


def _parse_custom(raw: object) -> list[CustomCommand]:
    commands: list[CustomCommand] = []
    if not isinstance(raw, list):
        return commands
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        command = str(item.get("command", "")).strip()
        if not key or not command:
            continue
        commands.append(
            CustomCommand(
                key=key,
                command=command,
                description=str(item.get("description", "")),
                confirm=bool(item.get("confirm", False)),
            )
        )
    return commands


def _parse_keys(raw: object) -> dict[str, dict[str, str]]:
    """``[keys.<panel>] action = "key"`` tables → overrides dict."""
    if not isinstance(raw, dict):
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for panel, actions in raw.items():
        if not isinstance(actions, dict):
            continue
        table = {str(action): str(key) for action, key in actions.items() if action and key}
        if table:
            overrides[str(panel)] = table
    return overrides


def load_config(path: Path | None = None) -> Config:
    """Read config from ``path`` (default ``CONFIG_PATH``), tolerating errors."""
    target = path or CONFIG_PATH
    try:
        raw = tomllib.loads(target.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return Config()

    _ValueT = TypeVar("_ValueT")

    def get(key: str, default: _ValueT, cast: Callable[..., _ValueT]) -> _ValueT:
        value = raw.get(key, default)
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    return Config(
        # The layout CSS enforces min-width: 30 on #sidebar; keep the
        # clamp at the same floor so config cannot promise what CSS denies.
        sidebar_width=max(30, get("sidebar_width", 46, int)),
        poll_seconds=min(60.0, max(0.5, get("poll_seconds", 2.0, float))),
        log_limit=max(50, get("log_limit", 500, int)),
        custom_commands=tuple(_parse_custom(raw.get("custom"))),
        key_overrides=_parse_keys(raw.get("keys")),
    )
