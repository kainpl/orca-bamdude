"""The plugin's durable record of what it sent (spec r2 §5.3, §8.4).

One JSON file per operation, written atomically (temp file, fsync, replace), inside a folder that belongs to one
namespace -- this server's connector id, its storage epoch and the API key's identity. Another server, a restored
server or another key never reads this history as its own.

The operation id is created and written BEFORE the first request: a lost answer is reconciled by asking for that
same id, never by sending again under a new one. A record's ``phase`` says how far the plugin got -- its upload and
submit state; ``unknown`` means a request went out and its answer did not come back.

Schema (host spec r2.2 §7). Every record carries ``schema``. A record of the first format (no ``schema``) is read and
migrated in memory, never rewritten by the read. A record of a newer format is listed with its unknown fields
ignored, and never overwritten by this code. Each namespace folder names its namespace in ``namespace.json``, so the
history shows another server's or another epoch's records -- as unknown, never sent again: a record remembers the
folder it was read from, and a journal saves only its own.

Ownership (Codex review R1). OrcaSlicer's old and new processes overlap for seconds on every restart, and a host
callback may hold a record it read a while ago. So a record carries a ``revision``, and every write compares it with
the file on disk under a lock on the namespace folder that every process honours (``.lock``; ``msvcrt`` on Windows,
``flock`` elsewhere): a save from a stale snapshot is refused (``stale``), a file now of a newer format is never
overwritten (``newer_schema``) and a damaged one never replaced (``damaged``) -- each without changing a byte.
``update`` reads, changes and writes under that lock: the way to note a milestone so it is noted once.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, fields

PHASES = ("prepared", "uploading", "uploaded", "submitting", "accepted", "unknown", "finished", "upload_only")
SCHEMA = 2
ORIGINS = ("native", "manual")
# The three identity spaces a notification is deduplicated in (host spec r2.2 §11 S3).
MILESTONE_KINDS = ("job", "assignment", "execution")
NAMESPACE_FILE = "namespace.json"
LOCK_FILE = ".lock"
LOCK_TIMEOUT = 5.0


class JournalError(ValueError):
    """A record this code must not write: a newer format, another namespace's record, a stale snapshot, a damaged
    or missing file, a lock another writer holds too long (``busy``), or a value outside the schema."""


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


class _FolderLock:
    """The namespace folder's write lock: one thread of this process, then the OS lock other processes honour."""

    def __init__(self, folder: str, timeout: float):
        self._path = os.path.join(folder, LOCK_FILE)
        self._timeout = timeout
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(os.path.normcase(self._path), threading.Lock())
        self._handle = None

    def __enter__(self):
        deadline = time.monotonic() + self._timeout
        if not self._thread_lock.acquire(timeout=self._timeout):
            raise JournalError("busy")
        try:
            self._handle = open(self._path, "a+b")
            while not _try_lock(self._handle):
                if time.monotonic() >= deadline:
                    raise JournalError("busy")
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


def _read_durable(path: str) -> dict | None:
    """The record as it is on disk now; ``None`` when there is none. A file that does not parse is never replaced."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise JournalError("damaged") from exc
    if not isinstance(payload, dict):
        raise JournalError("damaged")
    return payload


def _check_durable(payload: dict | None, expected_revision: int) -> None:
    """Refuse a write over a newer format or over a revision the writer has not seen."""
    if payload is None:
        if expected_revision != 0:
            raise JournalError("missing")
        return
    schema = payload.get("schema", 1)
    if not isinstance(schema, int) or schema > SCHEMA:
        raise JournalError("newer_schema")
    if payload.get("revision", 0) != expected_revision:
        raise JournalError("stale")


def _folder_name(namespace: tuple[str, str, str]) -> str:
    joined = "\n".join(namespace).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:32]


def _write_atomic(path: str, payload: dict) -> None:
    partial = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


@dataclass
class Record:
    operation_id: str
    phase: str
    created_at: float
    filename: str
    source_sha256: str  # the hash of the plugin's staged copy -- the bytes that were sent
    library_file_id: int | None = None
    body: dict | None = None
    result: dict | None = None
    error: str | None = None
    schema: int = SCHEMA
    origin: str | None = None  # "native" (Orca's Print/Send) or "manual" (the tab); unknown for the first format
    plates: list[int] = field(default_factory=list)
    target: dict | None = None
    milestones: dict[str, list[str]] = field(default_factory=dict)
    # How many times the record was written; a save must name the revision it read. The first format has none (0).
    revision: int = 0
    # Where the record was read from; never stored. A journal saves only a record of its own folder.
    folder: str | None = field(default=None, compare=False, repr=False)

    def as_dict(self) -> dict:
        payload = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "folder"}
        payload["plates"] = list(self.plates)
        payload["milestones"] = {key: list(seen) for key, seen in self.milestones.items()}
        return payload

    def note_milestone(self, kind: str, identity: str, milestone: str) -> bool:
        """True the first time this milestone is seen for this identity -- the moment to notify, once."""
        if kind not in MILESTONE_KINDS or not identity or not milestone:
            raise JournalError("milestone")
        seen = self.milestones.setdefault(f"{kind}:{identity}", [])
        if milestone in seen:
            return False
        seen.append(milestone)
        return True


_FIELDS = frozenset(f.name for f in fields(Record)) - {"folder"}


def _record(payload: dict, folder: str) -> Record:
    """A stored record in the current shape: the first format migrated, a newer one's unknown fields ignored."""
    if not isinstance(payload, dict):
        raise TypeError("record")
    known = {key: value for key, value in payload.items() if key in _FIELDS}
    schema = known.get("schema", 1)
    if not isinstance(schema, int) or schema < 1:
        raise TypeError("schema")
    if schema < SCHEMA:
        known["schema"] = SCHEMA  # the first format: every new field takes its default
    record = Record(**known)
    if record.origin is not None and record.origin not in ORIGINS:
        raise TypeError("origin")
    if not isinstance(record.revision, int) or record.revision < 0:
        raise TypeError("revision")
    record.folder = folder
    return record


def _read_namespace(folder: str) -> tuple[str, str, str] | None:
    try:
        with open(os.path.join(folder, NAMESPACE_FILE), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None  # a folder of the first format, or a damaged file: the namespace is not known
    parts = value.get("namespace") if isinstance(value, dict) else None
    if isinstance(parts, list) and len(parts) == 3 and all(isinstance(part, str) for part in parts):
        return parts[0], parts[1], parts[2]
    return None


@dataclass(frozen=True)
class NamespaceInfo:
    folder: str
    namespace: tuple[str, str, str] | None


def _journal_root(root: str) -> str:
    return os.path.join(root, "journal")


def list_namespaces(root: str) -> list[NamespaceInfo]:
    """Every namespace folder under the plugin's storage, with the namespace it names (``None`` when unknown)."""
    base = _journal_root(root)
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        folder = os.path.join(base, name)
        if os.path.isdir(folder):
            found.append(NamespaceInfo(folder, _read_namespace(folder)))
    return found


def _read_folder(folder: str) -> list[Record]:
    found = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json") or name == NAMESPACE_FILE:
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                found.append(_record(json.load(handle), folder))
        except (OSError, ValueError, TypeError):
            continue  # a damaged record is skipped, never rewritten
    return sorted(found, key=lambda r: r.created_at, reverse=True)


def foreign_records(root: str, namespace: tuple[str, str, str]) -> list[tuple[tuple[str, str, str] | None, Record]]:
    """Records of every other namespace, newest first: shown as unknown, never sent again from here."""
    ours = os.path.join(_journal_root(root), _folder_name(namespace))
    found = []
    for info in list_namespaces(root):
        if os.path.normcase(info.folder) != os.path.normcase(ours):
            found.extend((info.namespace, record) for record in _read_folder(info.folder))
    return sorted(found, key=lambda pair: pair[1].created_at, reverse=True)


class Journal:
    def __init__(self, root: str, namespace: tuple[str, str, str], *, lock_timeout: float = LOCK_TIMEOUT):
        self.folder = os.path.join(_journal_root(root), _folder_name(namespace))
        self._lock_timeout = lock_timeout
        os.makedirs(self.folder, exist_ok=True)
        if _read_namespace(self.folder) != tuple(namespace):
            _write_atomic(os.path.join(self.folder, NAMESPACE_FILE), {"namespace": list(namespace)})

    def _path(self, operation_id: str) -> str:
        str(uuid.UUID(operation_id))  # a path component only ever comes from a UUID
        return os.path.join(self.folder, f"{operation_id}.json")

    def create(
        self,
        filename: str,
        source_sha256: str,
        *,
        origin: str | None = None,
        plates=(),
        target: dict | None = None,
    ) -> Record:
        if origin is not None and origin not in ORIGINS:
            raise JournalError("origin")
        record = Record(
            operation_id=str(uuid.uuid4()),
            phase="prepared",
            created_at=time.time(),
            filename=filename,
            source_sha256=source_sha256,
            origin=origin,
            plates=[int(plate) for plate in plates],
            target=dict(target) if target is not None else None,
            folder=self.folder,
        )
        self.save(record)
        return record

    def _lock(self) -> _FolderLock:
        return _FolderLock(self.folder, self._lock_timeout)

    @staticmethod
    def _write(path: str, record: Record) -> None:
        payload = record.as_dict()
        payload["revision"] = record.revision + 1
        _write_atomic(path, payload)
        record.revision += 1

    def save(self, record: Record) -> None:
        """Write this snapshot -- only if the file on disk is still the revision it was read at."""
        if record.phase not in PHASES:
            raise ValueError(record.phase)
        if record.schema > SCHEMA:
            raise JournalError("newer_schema")  # written by a newer plugin: this code never overwrites it
        if record.folder is not None and os.path.normcase(record.folder) != os.path.normcase(self.folder):
            raise JournalError("foreign_namespace")  # another namespace's record is shown, never written here
        path = self._path(record.operation_id)
        with self._lock():
            _check_durable(_read_durable(path), record.revision)
            self._write(path, record)
        record.folder = self.folder

    def update(self, operation_id: str, mutate) -> Record:
        """Read the record as it is on disk, let ``mutate`` change it, write it -- all under the folder's lock.

        Nothing is written when ``mutate`` raises or changes nothing (a milestone already noted).
        """
        path = self._path(operation_id)
        with self._lock():
            payload = _read_durable(path)
            if payload is None:
                raise JournalError("missing")
            _check_durable(payload, payload.get("revision", 0))
            try:
                record = _record(payload, self.folder)
            except TypeError as exc:
                raise JournalError("damaged") from exc
            before = record.as_dict()
            mutate(record)
            if record.phase not in PHASES:
                raise ValueError(record.phase)
            if record.as_dict() != before:
                self._write(path, record)
        return record

    def load(self, operation_id: str) -> Record | None:
        try:
            with open(self._path(operation_id), encoding="utf-8") as handle:
                return _record(json.load(handle), self.folder)
        except FileNotFoundError:
            return None

    def records(self) -> list[Record]:
        return _read_folder(self.folder)

    def pending(self) -> list[Record]:
        """What needs reconciliation: a request went out and the final answer is not recorded."""
        return [r for r in self.records() if r.phase in ("uploading", "submitting", "unknown", "accepted")]
