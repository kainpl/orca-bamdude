"""The export folder the tab sends from (spec r2 §8.4).

OrcaSlicer gives a plugin no file picker, so the plugin lists the configured folder itself -- bounded in count and
depth, plain ``.3mf`` files only, no symbolic link, junction or other reparse point followed -- and the page gets an
opaque, one-time, short-lived token per file, bound to the connection generation, the folder, the file's relative
path, size and modification time. A path from the page is never accepted. At send time the token is spent first, and
the file must still be a plain file inside the folder (its real path; a link that leads out is refused) with the size
and time it was listed with. Staging then checks the file against change again while it copies it.
"""

from __future__ import annotations

import os
import secrets
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

MAX_FILES = 200
MAX_DEPTH = 3  # the folder itself and two levels below it
TOKEN_SECONDS = 600.0
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ExportError(Exception):
    def __init__(self, code: str, relative: str | None = None):
        super().__init__(code)
        self.code = code
        self.relative = relative  # the file the token was issued for, when it is known: the refusal names it


@dataclass(frozen=True)
class Listed:
    token: str
    relative: str
    size: int
    modified: float  # seconds since the epoch, for display


@dataclass
class _Issued:
    root: str
    relative: str
    size: int
    mtime_ns: int
    generation: int
    expires: float
    used: bool = False


def _linked(info: os.stat_result) -> bool:
    """A symbolic link, or on Windows any reparse point (a junction among them)."""
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _canonical(root: str) -> str:
    return os.path.normcase(os.path.realpath(root))


class Exports:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_files: int = MAX_FILES):
        self._clock = clock
        self._max_files = max_files
        self._lock = threading.Lock()
        self._issued: dict[str, _Issued] = {}

    def list(self, root: str, *, generation: int) -> list[Listed]:
        """The folder's plates, each with a fresh token; every earlier token is forgotten."""
        try:
            info = os.lstat(root)
        except OSError:
            return []
        if not stat.S_ISDIR(info.st_mode) or _linked(info):
            return []
        canonical = _canonical(root)
        found: list[tuple[str, os.stat_result]] = []
        self._walk(root, "", 1, found)
        expires = self._clock() + TOKEN_SECONDS
        listed, issued = [], {}
        for relative, entry in found:
            token = secrets.token_urlsafe(24)
            issued[token] = _Issued(canonical, relative, entry.st_size, entry.st_mtime_ns, generation, expires)
            listed.append(Listed(token, relative, entry.st_size, entry.st_mtime))
        with self._lock:
            self._issued = issued
        return listed

    def _walk(self, folder: str, prefix: str, depth: int, found: list) -> None:
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if len(found) >= self._max_files:
                return
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _linked(info):
                continue  # a link, a junction or another reparse point is never followed
            relative = os.path.join(prefix, entry.name) if prefix else entry.name
            if stat.S_ISDIR(info.st_mode):
                if depth < MAX_DEPTH:
                    self._walk(entry.path, relative, depth + 1, found)
            elif stat.S_ISREG(info.st_mode) and entry.name.lower().endswith(".3mf"):
                found.append((relative, info))

    def resolve(self, token: str, root: str, generation: int) -> str:
        """The real path of the file ``token`` was issued for -- or :class:`ExportError`. The token is spent."""
        with self._lock:
            issued = self._issued.get(token)
            if issued is None:
                raise ExportError("token_unknown")
            if issued.used:
                raise ExportError("token_used", issued.relative)
            issued.used = True
        if self._clock() > issued.expires:
            raise ExportError("token_expired", issued.relative)
        if issued.generation != generation or issued.root != _canonical(root):
            raise ExportError("token_stale", issued.relative)
        path = os.path.join(issued.root, issued.relative)
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise ExportError("missing", issued.relative) from exc
        if _linked(info) or not stat.S_ISREG(info.st_mode):
            raise ExportError("not_a_file", issued.relative)
        real = os.path.normcase(os.path.realpath(path))
        if os.path.commonpath([real, issued.root]) != issued.root:
            raise ExportError(
                "outside_root", issued.relative
            )  # a folder on the way was swapped for a link that leads out
        if info.st_size != issued.size or info.st_mtime_ns != issued.mtime_ns:
            raise ExportError("changed", issued.relative)
        return os.path.realpath(path)
