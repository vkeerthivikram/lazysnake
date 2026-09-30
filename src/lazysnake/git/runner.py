"""Async subprocess wrapper around the git CLI.

All lazysnake git access goes through :class:`Git`.  Commands run via
``asyncio`` subprocesses so the Textual UI never blocks, output is decoded
with ``surrogateescape`` so odd-but-valid filenames round-trip back to git,
and the environment is pinned so git never tries to page or prompt.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, overload

DEFAULT_TIMEOUT = 60.0

_READ_CHUNK = 64 * 1024
_CLEANUP_TIMEOUT = 1.0
_CLEANUP_CANCEL_WAIT = 0.1
_STDERR_CAPTURE_LIMIT = 64 * 1024

_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


def _spawn_options() -> Any:
    """Isolate POSIX commands in a killable session; Windows keeps its default."""
    return {"start_new_session": True} if os.name == "posix" else {}


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and every descendant it spawned.

    POSIX: the child runs in its own session (``start_new_session``), so
    signalling the group leader's PID kills the whole group — including
    grandchildren spawned before the shell/git leader exited.

    Windows: there are no POSIX process groups, so ``taskkill /T /F``
    terminates the entire descendant tree by PID. A Job Object with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` would also cover a lazysnake
    crash; taskkill covers the explicit timeout/cancellation paths
    without ctypes or asyncio handle plumbing.
    """
    pid = getattr(proc, "pid", None)
    if pid is not None:
        if os.name == "posix":
            try:
                os.killpg(pid, signal.SIGKILL)
                return
            except OSError:
                pass
        elif os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _close_process_pipes(proc: asyncio.subprocess.Process) -> None:
    transport = getattr(proc, "_transport", None)
    close = getattr(transport, "close", None)
    if close is not None:
        try:
            close()
        except OSError:
            pass


async def _drain_and_reap(proc: asyncio.subprocess.Process) -> None:
    async def drain(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(_READ_CHUNK):
                pass

    await asyncio.gather(drain(proc.stdout), drain(proc.stderr), proc.wait())


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    retained = bytearray()
    truncated = False
    while chunk := await stream.read(_READ_CHUNK):
        room = max(0, limit - len(retained))
        retained.extend(chunk[:room])
        truncated = truncated or len(chunk) > room
    return bytes(retained), truncated


async def _feed_stdin(stream: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if stream is None or data is None:
        return
    try:
        stream.write(data)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
    try:
        await stream.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass


async def _communicate_capped(
    proc: asyncio.subprocess.Process, data: bytes | None, stdout_limit: int
) -> tuple[bytes, bool, bytes, bool]:
    stdout_result, stderr_result, _, _ = await asyncio.gather(
        _read_capped(proc.stdout, stdout_limit),
        _read_capped(proc.stderr, _STDERR_CAPTURE_LIMIT),
        proc.wait(),
        _feed_stdin(proc.stdin, data),
    )
    stdout, stdout_truncated = stdout_result
    stderr, stderr_truncated = stderr_result
    return stdout, stdout_truncated, stderr, stderr_truncated


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap a subprocess without allowing inherited pipes to hang cleanup."""
    _kill_process_group(proc)
    if os.name != "posix":
        try:
            await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_TIMEOUT)
        except TimeoutError:
            pass
        finally:
            _close_process_pipes(proc)
        return

    task = asyncio.create_task(_drain_and_reap(proc))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_CLEANUP_TIMEOUT)
    except TimeoutError:
        _close_process_pipes(proc)
        task.cancel()
        await asyncio.wait({task}, timeout=_CLEANUP_CANCEL_WAIT)
    except asyncio.CancelledError:
        _close_process_pipes(proc)
        task.cancel()
        await asyncio.wait({task}, timeout=_CLEANUP_CANCEL_WAIT)
        raise


_stop_and_reap = terminate_process


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


def _is_lock_refusal(stderr: str) -> bool:
    """Detect git's atomic refusal to create index.lock.

    On Windows the previous git's lock deletion can linger (antivirus
    holds the handle), so the NEXT sequential git sees a stale entry and
    refuses before doing any work. Refusal is atomic and upfront, so a
    bounded retry is safe; a lock genuinely held by another process also
    refuses, and the retry simply outlives a few of those too.
    """
    return "index.lock" in stderr and "File exists" in stderr


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
            # Read-only commands (status, diff, log) must never refresh the
            # index stat cache under index.lock — the poll timer runs them
            # constantly and lock churn collides with real mutations
            # (worst on Windows, where lock-file deletion can linger).
            "GIT_OPTIONAL_LOCKS": "0",
            "TERM": env.get("TERM") or "dumb",
        }
    )
    return env


class Git:
    """Thin async wrapper around the git CLI for one repository."""

    def __init__(self, repo_root: Path | str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.repo_root = Path(repo_root)
        self.timeout = timeout
        self._git_dir: Path | None = None

    async def git_dir(self) -> Path:
        """Absolute path of this repository's git directory.

        In a linked worktree ``.git`` is a *file* pointing at the common
        git dir, so ``repo_root/.git/<name>`` lookups silently misreport
        sequencer state there; ``git rev-parse`` resolves both layouts.
        The result is cached on the instance, so refresh-time callers pay
        one extra rev-parse per ``Git`` lifetime, not per refresh. The
        cache cannot go stale: a ``Git`` is bound to one repo root, and
        switching repositories relaunches the process (``switch_repo``).
        """
        if self._git_dir is None:
            try:
                out = await self.run("rev-parse", "--absolute-git-dir")
            except GitError:
                # Repo too broken to ask git: assume the plain layout and
                # do not cache, so a later call can retry.
                return self.repo_root / ".git"
            self._git_dir = Path(out.strip())
        return self._git_dir

    async def state_file(self, name: str) -> Path:
        """On-disk path of a git state file (MERGE_HEAD, BISECT_LOG, ...)."""
        return (await self.git_dir()) / name

    @classmethod
    async def discover(
        cls, start_dir: Path | str = ".", *, timeout: float = DEFAULT_TIMEOUT
    ) -> Git:
        """Locate the repository root from ``start_dir`` and return a runner."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--show-toplevel",
            cwd=str(start_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_spawn_options(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            await terminate_process(proc)
            raise GitError(
                ["rev-parse", "--show-toplevel"],
                -1,
                "",
                f"timed out after {timeout}s",
            ) from None
        except asyncio.CancelledError:
            await terminate_process(proc)
            raise
        if proc.returncode != 0:
            assert proc.returncode is not None  # communicate() reaped the child
            raise GitError(
                ["rev-parse", "--show-toplevel"],
                proc.returncode,
                stdout.decode(_ENCODING, _ERRORS),
                stderr.decode(_ENCODING, _ERRORS),
            )
        root = stdout.decode(_ENCODING, _ERRORS).strip()
        return cls(Path(root))

    @overload
    async def run(
        self,
        *args: str,
        check: bool = True,
        env_extra: Mapping[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> str: ...

    @overload
    async def run(
        self,
        *args: str,
        check: bool = True,
        env_extra: Mapping[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
        max_output_bytes: int,
    ) -> tuple[str, bool]: ...

    async def run(
        self,
        *args: str,
        check: bool = True,
        env_extra: Mapping[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
        max_output_bytes: int | None = None,
    ) -> str | tuple[str, bool]:
        """Run ``git <args>`` and return decoded stdout.

        ``input`` (if given) is piped to the command's stdin — used for
        feeding hand-built patches to ``git apply``.
        With ``max_output_bytes``, return ``(stdout, truncated)`` while
        draining the remaining output without retaining it.
        Raises :class:`GitError` when the command fails, unless
        ``check=False`` (then raw stdout is returned whatever the exit
        code was).

        Stale index.lock refusals (see :func:`_is_lock_refusal`) are
        retried a bounded number of times before surfacing.
        """
        if max_output_bytes is not None and max_output_bytes < 0:
            raise ValueError("max_output_bytes must not be negative")
        for attempt in range(3):
            try:
                return await self._run_once(
                    *args,
                    check=check,
                    env_extra=env_extra,
                    timeout=timeout,
                    input=input,
                    max_output_bytes=max_output_bytes,
                )
            except GitError as err:
                if attempt < 2 and _is_lock_refusal(err.stderr):
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def _run_once(
        self,
        *args: str,
        check: bool = True,
        env_extra: Mapping[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
        max_output_bytes: int | None = None,
    ) -> str | tuple[str, bool]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_root),
            stdin=asyncio.subprocess.PIPE if input is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(env_extra),
            **_spawn_options(),
        )
        stdin_bytes = input.encode(_ENCODING, _ERRORS) if input is not None else None
        try:
            if max_output_bytes is None:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(stdin_bytes), timeout=timeout or self.timeout
                )
                stdout_truncated = False
            else:
                stdout_b, stdout_truncated, stderr_b, stderr_truncated = await asyncio.wait_for(
                    _communicate_capped(proc, stdin_bytes, max_output_bytes),
                    timeout=timeout or self.timeout,
                )
                if stderr_truncated:
                    stderr_b += b"\n...[stderr truncated]"
        except TimeoutError:
            await terminate_process(proc)
            raise GitError(
                list(args), -1, "", f"timed out after {timeout or self.timeout}s"
            ) from None
        except asyncio.CancelledError:
            await terminate_process(proc)
            raise
        stdout = stdout_b.decode(_ENCODING, _ERRORS)
        stderr = stderr_b.decode(_ENCODING, _ERRORS)
        if proc.returncode != 0 and check:
            assert proc.returncode is not None  # communicate() reaped the child
            raise GitError(list(args), proc.returncode, stdout, stderr)
        if max_output_bytes is not None:
            return stdout, stdout_truncated
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
        capture_output: bool = True,
        max_line_bytes: int | None = None,
    ) -> tuple[int, str]:
        """Run ``git <args>`` and stream stdout/stderr as they arrive.

        ``on_line(text, is_stderr)`` fires per line while the command runs.
        Output is retained by default; set ``capture_output=False`` to drain
        without storing it. ``max_line_bytes`` bounds each emitted line while
        still draining its remainder. Returns ``(returncode, combined_output)``.
        Non-zero exits are reported in the return code, not raised.
        Stale index.lock refusals are retried a bounded number of times.
        """
        if max_line_bytes is not None and max_line_bytes < 1:
            raise ValueError("max_line_bytes must be positive")
        for attempt in range(3):
            code, combined = await self._run_streaming_once(
                *args,
                on_line=on_line,
                env_extra=env_extra,
                timeout=timeout,
                capture_output=capture_output,
                max_line_bytes=max_line_bytes,
            )
            if attempt < 2 and code != 0 and _is_lock_refusal(combined):
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            return code, combined
        raise AssertionError("unreachable")  # pragma: no cover

    async def _run_streaming_once(
        self,
        *args: str,
        on_line: Callable[[str, bool], None] | None = None,
        env_extra: Mapping[str, str] | None = None,
        timeout: float | None = None,
        capture_output: bool = True,
        max_line_bytes: int | None = None,
    ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(env_extra),
            **_spawn_options(),
        )
        combined: list[str] = []

        def _emit(raw: bytes, is_err: bool, truncated: bool = False) -> None:
            text = raw.decode(_ENCODING, _ERRORS)
            if truncated:
                text += " ... [line truncated]"
            if capture_output:
                combined.append(text)
            if on_line is not None:
                on_line(text, is_err)

        async def pump(stream: asyncio.StreamReader | None, is_err: bool) -> None:
            if stream is None:
                return
            buf = bytearray()
            truncated = False
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    newline = chunk.find(b"\n", offset)
                    end = len(chunk) if newline < 0 else newline
                    segment = chunk[offset:end]
                    if max_line_bytes is None:
                        buf.extend(segment)
                    else:
                        room = max(0, max_line_bytes - len(buf))
                        buf.extend(segment[:room])
                        truncated = truncated or len(segment) > room
                    if newline < 0:
                        break
                    _emit(bytes(buf), is_err, truncated)
                    buf.clear()
                    truncated = False
                    offset = newline + 1
            if buf or truncated:
                _emit(bytes(buf), is_err, truncated)

        try:
            await asyncio.wait_for(
                asyncio.gather(pump(proc.stdout, False), pump(proc.stderr, True), proc.wait()),
                timeout=timeout or self.timeout,
            )
        except TimeoutError:
            await terminate_process(proc)
            raise GitError(
                list(args), -1, "", f"timed out after {timeout or self.timeout}s"
            ) from None
        except asyncio.CancelledError:
            await terminate_process(proc)
            raise
        return proc.returncode or 0, "\n".join(combined)
