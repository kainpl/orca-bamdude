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
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, fields

PHASES = ("prepared", "uploading", "uploaded", "submitting", "accepted", "unknown", "finished", "upload_only")
SCHEMA = 2
ORIGINS = ("native", "manual")
# The three identity spaces a notification is deduplicated in (host spec r2.2 §11 S3).
MILESTONE_KINDS = ("job", "assignment", "execution")
NAMESPACE_FILE = "namespace.json"


class JournalError(ValueError):
    """A record this code must not write: a newer format, another namespace's record, or a value outside the schema."""


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
    def __init__(self, root: str, namespace: tuple[str, str, str]):
        self.folder = os.path.join(_journal_root(root), _folder_name(namespace))
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

    def save(self, record: Record) -> None:
        if record.phase not in PHASES:
            raise ValueError(record.phase)
        if record.schema > SCHEMA:
            raise JournalError("newer_schema")  # written by a newer plugin: this code never overwrites it
        if record.folder is not None and os.path.normcase(record.folder) != os.path.normcase(self.folder):
            raise JournalError("foreign_namespace")  # another namespace's record is shown, never written here
        record.folder = self.folder
        _write_atomic(self._path(record.operation_id), record.as_dict())

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
