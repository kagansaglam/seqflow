"""Single-instance lock based on an exclusively created PID file.

Two watchers on the same directory would demultiplex the same run twice. The lock
file is created with O_EXCL, so creation is atomic. A lock left behind by a crashed
process (its PID no longer exists) is taken over.

Known limit: two processes that find the same stale lock at the very same instant
can still race on removing it. That needs a crashed holder and two simultaneous
starts, so it is documented rather than solved.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from .exceptions import LockHeldError

# An empty lock file is usually a starter between create and write; leave it alone
# for this many seconds before calling it stale.
EMPTY_FILE_GRACE_SECONDS = 5.0


def pid_alive(pid: int) -> bool:
    """True if a process with this PID exists."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied  # exists, but not ours
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


class LockFile:
    def __init__(self, path: str | Path, *, alive: Callable[[int], bool] = pid_alive) -> None:
        self.path = Path(path)
        self._alive = alive
        self._held = False

    def _holder(self) -> int | None:
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                holder = self._holder()
                if holder is not None and self._alive(holder):
                    raise LockHeldError(
                        f"another seqflow process (pid {holder}) holds {self.path}"
                    ) from None
                if holder is None and self._age_seconds() < EMPTY_FILE_GRACE_SECONDS:
                    raise LockHeldError(f"{self.path} is being created by another process") from None
                self.path.unlink(missing_ok=True)  # stale: holder is gone
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            self._held = True
            return
        raise LockHeldError(f"could not take over stale lock {self.path}")

    def _age_seconds(self) -> float:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return float("inf")

    def release(self) -> None:
        """Remove the lock file, but only if this process created it."""
        if self._held and self._holder() == os.getpid():
            self.path.unlink(missing_ok=True)
        self._held = False

    def __enter__(self) -> LockFile:  # noqa: PYI034 - typing.Self needs Python 3.11
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
