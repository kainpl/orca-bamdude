"""A write lock on a file that every OrcaSlicer process honours, and every thread of this one.

OrcaSlicer's old and new processes overlap for seconds on every restart, and a re-activated plugin is imported again
in the same process while the old load's threads finish (host spec r2.3 §4.5). So a lock is taken twice: first a
thread lock of this module instance, then the operating system's lock on the file -- ``msvcrt`` on Windows (per
handle), ``flock`` elsewhere (per open file description). The OS lock is what separates two processes and two
imports of this module in one process, whose thread locks are different objects.
"""

from __future__ import annotations

import os
import threading
import time

if os.name == "nt":
    import msvcrt

    def _try_lock(handle) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class LockBusy(Exception):
    """Another writer held the lock past the timeout."""


class FileLock:
    """The write lock of ``path``: one thread of this process, then the OS lock other processes honour.

    ``busy`` makes the exception raised when the timeout passes, so a caller keeps its own error type.
    """

    def __init__(self, path: str, timeout: float, busy=LockBusy):
        self._path = path
        self._timeout = timeout
        self._busy = busy
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(os.path.normcase(path), threading.Lock())
        self._handle = None

    def __enter__(self):
        deadline = time.monotonic() + self._timeout
        if not self._thread_lock.acquire(timeout=self._timeout):
            raise self._busy()
        try:
            self._handle = open(self._path, "a+b")
            while not _try_lock(self._handle):
                if time.monotonic() >= deadline:
                    raise self._busy()
                time.sleep(0.02)
        except BaseException:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None
            self._thread_lock.release()
