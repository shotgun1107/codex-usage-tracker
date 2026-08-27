"""Cross-process exclusion for local collect and sync mutations."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType


class ApplicationLockError(RuntimeError):
    """Another mutating command is already using the local state."""


class ApplicationLock:
    def __init__(self, state_database: str | Path) -> None:
        state_path = Path(state_database).expanduser().resolve()
        self.path = state_path.with_name(f"{state_path.name}.lock")
        self._handle = None

    def __enter__(self) -> ApplicationLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _lock(handle.fileno())
        except (OSError, BlockingIOError) as error:
            if "handle" in locals():
                handle.close()
            raise ApplicationLockError(
                "another collect or sync command is already running"
            ) from error
        self._handle = handle
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            _unlock(self._handle.fileno())
        finally:
            self._handle.close()
            self._handle = None


if os.name == "nt":
    import msvcrt

    def _lock(file_descriptor: int) -> None:
        msvcrt.locking(file_descriptor, msvcrt.LK_NBLCK, 1)

    def _unlock(file_descriptor: int) -> None:
        msvcrt.locking(file_descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(file_descriptor: int) -> None:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(file_descriptor: int) -> None:
        fcntl.flock(file_descriptor, fcntl.LOCK_UN)
