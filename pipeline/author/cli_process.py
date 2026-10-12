"""Shared process lifecycle for the reused author CLIs.

Extracted from the Arena Claude runner, whose timeout/cancellation cleanup
adapts Dreamliner's services/taskgen/app/utils/claude_cli_runner.py.
The caller must supply the sandbox; setting cwd does not isolate an agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import BinaryIO


def _linux_process(pid: int) -> tuple[int, int, int] | None:
    """Read identity/parent/group only; command lines and environments stay private."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[19]), int(fields[1]), int(fields[2])
    except (OSError, ValueError, IndexError):
        return None


class _LinuxDescendants:
    """Retain observed ancestry across setsid and reparenting, scoped to one call.

    This is process cleanup, not a sandbox. A daemon-owned Docker container is
    not a descendant, and a process orphaned before observation can be missed.
    """

    def __init__(self, pid: int):
        self.root = pid
        self.errors: list[dict] = []
        record = _linux_process(pid)
        self.owned = {pid: record[0]} if record is not None else {}

    def _error(self, pid: int, sig: int, exc: OSError) -> None:
        detail = {"pid": pid, "signal": int(sig), "error": type(exc).__name__, "errno": exc.errno}
        if detail not in self.errors:
            self.errors.append(detail)
            logging.getLogger(__name__).warning("Owned process cleanup: pid=%s signal=%s error=%s errno=%s",
                                               pid, int(sig), type(exc).__name__, exc.errno)

    def snapshot(self) -> None:
        pending = list(self.owned)
        visited = set()
        while pending:
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            record = _linux_process(parent)
            if record is None or record[0] != self.owned[parent]:
                self.owned.pop(parent, None)
                continue
            # A multithreaded CLI may launch tools from any thread.
            for path in Path(f"/proc/{parent}/task").glob("*/children"):
                try:
                    children = [int(value) for value in path.read_text().split()]
                except (OSError, ValueError):
                    continue
                for child in children:
                    if child <= 1 or child == os.getpid():
                        continue
                    identity = _linux_process(child)
                    if identity is not None and identity[1] == parent:
                        self.owned[child] = identity[0]
                        pending.append(child)

    def signal(self, pid: int, sig: int) -> None:
        expected = self.owned.get(pid)
        current = _linux_process(pid)
        if expected is None or current is None or current[0] != expected:
            return
        descriptor = None
        try:
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                try:
                    descriptor = os.pidfd_open(pid)
                except OSError as exc:
                    if exc.errno not in {errno.ENOSYS, errno.EINVAL}:
                        if not isinstance(exc, ProcessLookupError):
                            self._error(pid, sig, exc)
                        return
            # Check again after opening the handle; never signal a reused PID.
            current = _linux_process(pid)
            if current is None or current[0] != expected:
                return
            if descriptor is not None:
                signal.pidfd_send_signal(descriptor, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            # One inaccessible child cannot prevent cleanup of the CLI and its
            # other tools. Retain only process/error metadata, never arguments.
            self._error(pid, sig, exc)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def kill(self) -> None:
        # Observe before killing the CLI: otherwise its detached tools become
        # PPID 1 processes with no remaining ancestry to establish ownership.
        for index in range(4):
            before = dict(self.owned)
            self.snapshot()
            for pid in list(self.owned):
                self.signal(pid, signal.SIGSTOP)
            if index and self.owned == before:
                break
        # Preserve group cleanup for unobserved same-group tools, but only while
        # a matching owned process proves that this is still our original group.
        if any((current := _linux_process(pid)) is not None
               and current[0] == start and current[2] == self.root
               for pid, start in self.owned.items()):
            try:
                os.killpg(self.root, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                self._error(self.root, signal.SIGKILL, exc)
        for pid in reversed(list(self.owned)):
            self.signal(pid, signal.SIGKILL)


def _call_storage(log_dir: str | Path, cwd: str | Path | None) -> tuple[Path, Path]:
    """Keep CLI metadata and temporary files with their controller-owned call.

    Authentication/configuration stay in the CLI's normal locations. These
    directories organize task files; they do not provide an OS sandbox.
    """
    logs = Path(log_dir).absolute()
    scratch = (Path(cwd).absolute() if cwd is not None else logs.parent) / "scratch"
    for directory in (logs, scratch):
        if any(part.is_symlink() for part in (directory, *directory.parents)):
            raise ValueError(f"call storage must not use symlinks: {directory}")
    for directory in (logs, scratch):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger = logs / "model_calls.jsonl"
    if ledger.is_symlink():
        raise ValueError("model-call ledger must not use symlinks")
    return ledger, scratch


async def _logged_communication(proc: asyncio.subprocess.Process, prompt: str,
                                stdout_log: BinaryIO, stderr_log: BinaryIO) -> tuple[bytes, bytes]:
    """Drain both pipes concurrently so large outputs cannot block stdin/tools."""
    write_errors: list[OSError] = []

    async def pump(reader: asyncio.StreamReader, log: BinaryIO) -> bytes:
        chunks = []
        failed = False
        while chunk := await reader.read(64 * 1024):
            chunks.append(chunk)
            if not failed:
                try:
                    log.write(chunk)
                    log.flush()  # visible to operators before the agent exits
                except OSError as exc:
                    # Keep draining; abandoning a full pipe can deadlock cleanup.
                    write_errors.append(exc)
                    failed = True
        return b"".join(chunks)

    async def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # same early-exit behavior as Process.communicate
        finally:
            proc.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await proc.stdin.wait_closed()

    assert proc.stdout is not None and proc.stderr is not None
    tasks = [asyncio.create_task(pump(proc.stdout, stdout_log)),
             asyncio.create_task(pump(proc.stderr, stderr_log)),
             asyncio.create_task(feed())]
    try:
        stdout, stderr, _ = await asyncio.gather(*tasks)
        await proc.wait()
        if write_errors:
            raise OSError("could not persist the complete CLI logs") from write_errors[0]
        return stdout, stderr
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _open_logs(directory: str | Path, stack: contextlib.ExitStack) -> tuple[BinaryIO, BinaryIO]:
    root = Path(directory).absolute()
    for part in (root, *root.parents):
        if part.is_symlink():
            raise ValueError(f"log directory must not use symlinks: {part}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = []
    for name in ("stdout.log", "stderr.log"):
        # A retry must use a fresh directory, preserving the previous attempt.
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        logs.append(stack.enter_context(os.fdopen(fd, "wb")))
    return logs[0], logs[1]


async def _run_process(
    command: list[str],
    prompt: str,
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    log_dir: str | Path | None = None,
) -> tuple[int, str, str, float, bool]:
    """Send the brief on stdin and retain output, including on timeout.

Return (exit code, raw stdout, stderr, elapsed seconds, timed out).
Cancellation stops the process group and tracked Linux descendants, including
observed detached tools, and propagates to the caller. With log_dir,
stdout.log and stderr.log are fresh mode-600 files flushed as bytes arrive.
"""
    start = time.monotonic()
    with contextlib.ExitStack() as stack:
        logs = _open_logs(log_dir, stack) if log_dir is not None else None
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        descendants = _LinuxDescendants(proc.pid) if sys.platform.startswith("linux") else None

        async def _observe_descendants() -> None:
            assert descendants is not None
            while True:
                descendants.snapshot()
                await asyncio.sleep(0.05)

        observer = asyncio.create_task(_observe_descendants()) if descendants is not None else None
        communication = asyncio.create_task(
            _logged_communication(proc, prompt, *logs) if logs else
            proc.communicate(prompt.encode("utf-8")))

        async def _kill_and_reap() -> tuple[bytes, bytes]:
            # Descendant tools may hold the same output pipes as the CLI.
            try:
                if descendants is not None:
                    descendants.kill()
                elif os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            try:
                return await communication
            finally:
                await proc.wait()

        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                asyncio.shield(communication), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            stdout_b, stderr_b = await _kill_and_reap()
        except BaseException:
            # Includes cancellation and a failed log write. Never leave tools
            # running after the controller has stopped owning this invocation.
            with contextlib.suppress(Exception):
                await _kill_and_reap()
            raise
        else:
            # A finished CLI must not leave an observed background tool running.
            if descendants is not None:
                descendants.kill()
        finally:
            if observer is not None:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)

    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
        time.monotonic() - start,
        timed_out,
    )
