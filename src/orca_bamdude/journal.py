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

A record knows whether it is ``stored`` -- read from its file or written to it (Codex review r2, J1). A new one is
written only where no file is (``exists`` otherwise); a stored one only over a valid record of the same operation
(``missing`` when the file is gone, so a deleted record is never brought back, even one of the first format at
revision 0; ``damaged`` for what ``load`` would reject or a file naming another operation). ``update`` compares the
bytes it would write, never a shallow copy, so a change inside ``target``, ``body`` or ``result`` is kept (J2); it
never lets ``mutate`` change what the record is -- its operation id, revision or schema (``identity``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, fields

from . import locking
from .locking import FileLock

PHASES = ("prepared", "uploading", "uploaded", "submitting", "accepted", "unknown", "finished", "upload_only")
SCHEMA = 2
ORIGINS = ("native", "manual")
# The three identity spaces a notification is deduplicated in (host spec r2.2 §11 S3).
MILESTONE_KINDS = ("job", "assignment", "execution")
NAMESPACE_FILE = "namespace.json"
LOCK_FILE = ".lock"
LOCK_TIMEOUT = 5.0
# A record's attributes that live in memory only, never in its file.
_RUNTIME = frozenset({"folder", "stored"})


class JournalError(ValueError):
    """A record this code must not write: a newer format, another namespace's record, a stale snapshot, a damaged
    or missing file, a new record where a file already is (``exists``), a ``mutate`` that changed what the record is
    (``identity``), a lock another writer holds too long (``busy``), or a value outside the schema."""


def _read_durable(path: str) -> dict | None:
    """The record as it is on disk now; ``None`` when there is none. A file that does not parse is never replaced."""
    try:
        payload = json.loads(locking.read_text(path))  # a sharing violation is no damage: it is waited out
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise JournalError("damaged") from exc
    if not isinstance(payload, dict):
        raise JournalError("damaged")
    return payload


def _folder_name(namespace: tuple[str, str, str]) -> str:
    joined = "\n".join(namespace).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:32]


def _write_atomic(path: str, payload: dict) -> None:
    partial = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    locking.replace(partial, path)  # a lockless reader holding the record open is waited out


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
    # Read from its file or written to it; never stored. A record that is not is new: written only where no file is.
    stored: bool = field(default=False, compare=False, repr=False)

    def as_dict(self) -> dict:
        payload = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in _RUNTIME}
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


_FIELDS = frozenset(f.name for f in fields(Record)) - _RUNTIME


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
    record.stored = True
    return record


def _stored_record(payload: dict, operation_id: str, folder: str) -> Record:
    """The record on disk as a writer may replace it: a newer format stays read-only, and what ``load`` would reject
    -- or a file that names another operation -- is damaged, never replaced."""
    schema = payload.get("schema", 1)
    if isinstance(schema, int) and schema > SCHEMA:
        raise JournalError("newer_schema")
    try:
        record = _record(payload, folder)
    except TypeError as exc:
        raise JournalError("damaged") from exc
    if record.operation_id != operation_id:
        raise JournalError("damaged")
    return record


def _serialised(record: Record) -> str:
    """What a write would put on disk: the one comparison that sees a change inside a nested field."""
    return json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True)


def _read_namespace(folder: str) -> tuple[str, str, str] | None:
    try:
        value = json.loads(locking.read_text(os.path.join(folder, NAMESPACE_FILE)))
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
            found.append(_record(json.loads(locking.read_text(os.path.join(folder, name))), folder))
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
        self.root = root
        self.namespace = tuple(namespace)
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

    def _lock(self) -> FileLock:
        return FileLock(os.path.join(self.folder, LOCK_FILE), self._lock_timeout, busy=lambda: JournalError("busy"))

    @staticmethod
    def _write(path: str, record: Record) -> None:
        payload = record.as_dict()
        payload["revision"] = record.revision + 1
        _write_atomic(path, payload)
        record.revision += 1

    def save(self, record: Record) -> None:
        """Write this snapshot: a new record only where no file is, a stored one only over a valid record of the same
        operation that is still the revision it was read at."""
        if record.phase not in PHASES:
            raise ValueError(record.phase)
        if record.schema > SCHEMA:
            raise JournalError("newer_schema")  # written by a newer plugin: this code never overwrites it
        if record.folder is not None and os.path.normcase(record.folder) != os.path.normcase(self.folder):
            raise JournalError("foreign_namespace")  # another namespace's record is shown, never written here
        path = self._path(record.operation_id)
        with self._lock():
            payload = _read_durable(path)
            if not record.stored:
                if payload is not None:
                    raise JournalError("exists")
            elif payload is None:
                raise JournalError("missing")  # deleted since it was read: never brought back
            elif _stored_record(payload, record.operation_id, self.folder).revision != record.revision:
                raise JournalError("stale")
            self._write(path, record)
        record.folder = self.folder
        record.stored = True

    def update(self, operation_id: str, mutate) -> Record:
        """Read the record as it is on disk, let ``mutate`` change it, write it -- all under the folder's lock.

        Nothing is written when ``mutate`` raises, changes nothing (a milestone already noted) or changes what the
        record is -- its operation id, revision or schema.
        """
        path = self._path(operation_id)
        with self._lock():
            payload = _read_durable(path)
            if payload is None:
                raise JournalError("missing")
            record = _stored_record(payload, operation_id, self.folder)
            identity = (record.operation_id, record.revision, record.schema)
            before = _serialised(record)
            mutate(record)
            if (record.operation_id, record.revision, record.schema) != identity:
                raise JournalError("identity")
            if record.phase not in PHASES:
                raise ValueError(record.phase)
            if _serialised(record) != before:
                self._write(path, record)
        return record

    def load(self, operation_id: str) -> Record | None:
        try:
            return _record(json.loads(locking.read_text(self._path(operation_id))), self.folder)
        except FileNotFoundError:
            return None

    def records(self) -> list[Record]:
        return _read_folder(self.folder)

    def pending(self) -> list[Record]:
        """What needs reconciliation: a request went out and the final answer is not recorded."""
        return [r for r in self.records() if r.phase in ("uploading", "submitting", "unknown", "accepted")]
