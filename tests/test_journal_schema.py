"""The journal's schema (host spec r2.2 §7): versioned records, the first format still read, other namespaces listed.

- A record carries ``schema``. One written by the first format (no ``schema``) is read and migrated in memory; the
  file is not rewritten by a read.
- A record of a newer format is listed, its unknown fields ignored, and never overwritten by this code.
- Every namespace folder names its namespace; records of another namespace are shown, never written into ours.
- Milestones are kept per identity, so a notification already shown is not shown again after a restart.
"""

import json
import os
import uuid
from pathlib import Path

import pytest

from orca_bamdude.journal import SCHEMA, Journal, JournalError, foreign_records, list_namespaces

NS = ("server-a", "epoch-1", "key-1")

# A record exactly as the first format writes it (Journal.save of the plugin's first core, sorted keys).
FIRST_FORMAT = {
    "body": {"entries": []},
    "created_at": 1762300000.5,
    "error": None,
    "filename": "part.gcode.3mf",
    "library_file_id": 12,
    "operation_id": "0b8c6a43-5f0e-4ad5-9b8e-0e6d0b0f6c11",
    "phase": "uploaded",
    "result": None,
    "source_sha256": "a" * 64,
}


def _write(folder, payload):
    path = os.path.join(folder, f"{payload['operation_id']}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


@pytest.mark.parametrize("stored", [FIRST_FORMAT, {**FIRST_FORMAT, "schema": 1}], ids=["unnumbered", "numbered"])
def test_a_record_of_the_first_format_is_read_and_migrated_without_a_write(tmp_path, stored):
    journal = Journal(str(tmp_path), NS)
    path = _write(journal.folder, stored)
    before = os.path.getmtime(path), Path(path).read_text(encoding="utf-8")

    record = journal.load(FIRST_FORMAT["operation_id"])

    assert record.schema == SCHEMA
    assert (record.phase, record.library_file_id, record.source_sha256) == ("uploaded", 12, "a" * 64)
    assert (record.origin, record.plates, record.target, record.milestones) == (None, [], None, {})
    assert (os.path.getmtime(path), Path(path).read_text(encoding="utf-8")) == before
    assert [r.operation_id for r in journal.records()] == [FIRST_FORMAT["operation_id"]]


def test_a_new_record_carries_its_schema_and_what_was_sent(tmp_path):
    journal = Journal(str(tmp_path), NS)
    target = {"kind": "printer", "printer_id": 7, "printer_identity": str(uuid.uuid4())}

    record = journal.create("part.gcode.3mf", "b" * 64, origin="native", plates=[2], target=target)

    with open(os.path.join(journal.folder, f"{record.operation_id}.json"), encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["schema"] == SCHEMA
    assert (stored["origin"], stored["plates"], stored["target"]) == ("native", [2], target)
    assert stored["milestones"] == {}
    assert "folder" not in stored


@pytest.mark.parametrize("origin", ["printer", "", None])
def test_an_origin_is_native_or_manual(tmp_path, origin):
    journal = Journal(str(tmp_path), NS)
    if origin is None:
        assert journal.create("a.3mf", "c" * 64).origin is None
        return
    with pytest.raises(JournalError):
        journal.create("a.3mf", "c" * 64, origin=origin)


def test_a_record_of_a_newer_format_is_listed_but_never_overwritten(tmp_path):
    journal = Journal(str(tmp_path), NS)
    newer = {**FIRST_FORMAT, "schema": SCHEMA + 1, "phase": "finished", "something_new": {"x": 1}}
    path = _write(journal.folder, newer)
    before = Path(path).read_text(encoding="utf-8")

    [record] = journal.records()
    assert record.schema == SCHEMA + 1 and record.phase == "finished"
    record.phase = "unknown"
    with pytest.raises(JournalError):
        journal.save(record)

    assert Path(path).read_text(encoding="utf-8") == before


def test_a_milestone_is_noted_once_and_survives_a_restart(tmp_path):
    journal = Journal(str(tmp_path), NS)
    record = journal.create("a.3mf", "d" * 64, origin="manual")
    execution = str(uuid.uuid4())

    assert record.note_milestone("execution", execution, "first_layer") is True
    assert record.note_milestone("execution", execution, "first_layer") is False
    assert record.note_milestone("assignment", execution, "assigned") is True  # another key, another identity space
    journal.save(record)

    again = Journal(str(tmp_path), NS).load(record.operation_id)
    assert again.note_milestone("execution", execution, "first_layer") is False
    assert again.note_milestone("execution", execution, "completed") is True
    with pytest.raises(JournalError):
        again.note_milestone("printer", execution, "started")


def test_every_namespace_folder_is_listed_with_its_namespace(tmp_path):
    ours = Journal(str(tmp_path), NS)
    restored = Journal(str(tmp_path), ("server-a", "epoch-2", "key-1"))
    legacy = os.path.join(str(tmp_path), "journal", "f" * 32)  # a folder the first format made: no namespace file
    os.makedirs(legacy)
    _write(legacy, FIRST_FORMAT)

    listed = {info.folder: info.namespace for info in list_namespaces(str(tmp_path))}

    assert listed == {ours.folder: NS, restored.folder: ("server-a", "epoch-2", "key-1"), legacy: None}


def test_another_namespaces_records_are_shown_and_never_written_into_ours(tmp_path):
    ours = Journal(str(tmp_path), NS)
    restored = Journal(str(tmp_path), ("server-a", "epoch-2", "key-1"))
    theirs = restored.create("old.3mf", "e" * 64, origin="native")
    mine = ours.create("new.3mf", "f" * 64, origin="native")

    foreign = foreign_records(str(tmp_path), NS)

    assert [(namespace, record.operation_id) for namespace, record in foreign] == [
        (("server-a", "epoch-2", "key-1"), theirs.operation_id)
    ]
    [(_, shown)] = foreign
    shown.phase = "unknown"
    with pytest.raises(JournalError):
        ours.save(shown)
    assert ours.load(theirs.operation_id) is None
    assert [r.operation_id for r in ours.records()] == [mine.operation_id]
