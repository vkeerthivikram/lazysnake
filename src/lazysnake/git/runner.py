"""Async subprocess wrapper around the git CLI.

All lazysnake git access goes through :class:`Git`.  Commands run via
``asyncio`` subprocesses so the Textual UI never blocks, output is decoded
with ``surrogateescape`` so odd-but-valid filenames round-trip back to git,
and the environment is pinned so git never tries to page or prompt.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from pathlib import Path

DEFAULT_TIMEOUT = 60.0

_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


class GitError(RuntimeError):
    """A git command exited non-zero."""

    def __init__(
        self,
        git_args: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.git_args = list(git_args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(self._format())

    @property
    def command(self) -> str:
        return " ".join(["git", *self.git_args])

    def _format(self) -> str:
        detail = (self.stderr or self.stdout).strip()
        return f"{self.command}: {detail or f'exit code {self.returncode}'}"


def _base_env() -> dict[str, str]:
    """Environment that keeps git non-interactive and unpaged."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_EDITOR": "true",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "TERM": env.get("TERM") or "dumb",
        }
    )
    return env


class Git:
    """Thin async wrapper around the git CLI for one repository."""

    def __init__(self, repo_root: Path | str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.repo_root = Path(repo_root)
        self.timeout = timeout

    @classmethod
    async def discover(cls, start_dir: Path | str = ".") -> Git:
        """Locate the repository root from ``start_dir`` and return a runner."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--show-toplevel",
            cwd=str(start_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise GitError(
                ["rev-parse", "--show-toplevel"],
                proc.returncode,
                stdout.decode(_ENCODING, _ERRORS),
                stderr.decode(_ENCODING, _ERRORS),
            )
        root = stdout.decode(_ENCODING, _ERRORS).strip()
        return cls(Path(root))

    async def run(
        self,
        *args: str,
        check: bool = True,
        env_extra: Mapping[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> str:
        """Run ``git <args>`` and return decoded stdout.

        ``input`` (if given) is piped to the command's stdin — used for
        feeding hand-built patches to ``git apply``.
        Raises :class:`GitError` when the command fails, unless
        ``check=False`` (then the error text is returned instead).
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_root),
            stdin=asyncio.subprocess.PIPE if input is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(env_extra),
        )
        stdin_bytes = input.encode(_ENCODING, _ERRORS) if input is not None else None
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(stdin_bytes), timeout=timeout or self.timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise GitError(
                list(args), -1, "", f"timed out after {timeout or self.timeout}s"
            ) from None
        stdout = stdout_b.decode(_ENCODING, _ERRORS)
        stderr = stderr_b.decode(_ENCODING, _ERRORS)
        if proc.returncode != 0 and check:
            raise GitError(list(args), proc.returncode, stdout, stderr)
        return stdout

    def _env(self, env_extra: Mapping[str, str] | None) -> dict[str, str]:
        env = _base_env()
        if env_extra:
            env.update(env_extra)
        return env

    async def run_streaming(
        self,
        *args: str,
        on_line: Callable[[str, bool], None] | None = None,
        env_extra: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        """Run ``git <args>`` and stream stdout/stderr as they arrive.

        ``on_line(text, is_stderr)`` fires per line while the command runs —
        the UI appends remote progress to the command log live. Returns
        ``(returncode, combined_output)``. Non-zero exits are reported in
        the return code, not raised, so callers decide how to surface them.
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(env_extra),
        )
        combined: list[str] = []

        async def pump(stream: asyncio.StreamReader | None, is_err: bool) -> None:
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    return
                text = raw.decode(_ENCODING, _ERRORS).rstrip("\n")
                combined.append(text)
                if on_line is not None:
                    on_line(text, is_err)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    pump(proc.stdout, False), pump(proc.stderr, True), proc.wait()
                ),
                timeout=timeout or self.timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise GitError(
                list(args), -1, "", f"timed out after {timeout or self.timeout}s"
            ) from None
        return proc.returncode or 0, "\n".join(combined)
