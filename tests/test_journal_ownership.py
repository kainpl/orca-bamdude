"""Who may change a journal record (host spec r2.2 §7, Codex review R1).

Two OrcaSlicer processes overlap for seconds on every restart, and a host callback may hold a record read a while
ago. So every write compares the record's revision with the one on disk, under a lock on the namespace folder that
other processes honour too:

- a save from a stale snapshot is refused and changes no byte -- a milestone written meanwhile survives;
- a record whose file is now of a newer format is never overwritten, whatever the object in memory says;
- ``update`` reads, changes and writes under the lock, so two writers never lose each other's milestone;
- a new record is written only where no file is, and a record once read only over a valid record of the same
  operation: a deleted one is never brought back, and a file ``load`` rejects is never replaced (Codex review r2, J1);
- ``update`` compares what it would write, not a shallow copy, so a change inside a nested field is kept (J2).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from orca_bamdude.journal import SCHEMA, Journal, JournalError, Record

NS = ("server", "epoch", "actor")
SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_a_save_never_overwrites_a_newer_format_on_disk(tmp_path):
    journal = Journal(str(tmp_path), NS)
    old = journal.create("part.3mf", "a" * 64)
    path = Path(journal.folder) / f"{old.operation_id}.json"
    newer = {**old.as_dict(), "schema": SCHEMA + 1, "new_format_state": {"do_not_drop": True}}
    path.write_text(json.dumps(newer), encoding="utf-8")
    before = path.read_bytes()
    old.phase = "unknown"

    with pytest.raises(JournalError, match="newer_schema"):
        journal.save(old)

    assert path.read_bytes() == before


def test_a_stale_snapshot_is_refused_and_the_milestone_survives(tmp_path):
    journal = Journal(str(tmp_path), NS)
    first = journal.create("part.3mf", "b" * 64)
    stale = journal.load(first.operation_id)
    first.note_milestone("execution", "execution-1", "first_layer")
    journal.save(first)
    path = Path(journal.folder) / f"{first.operation_id}.json"
    before = path.read_bytes()
    stale.phase = "accepted"

    with pytest.raises(JournalError, match="stale"):
        journal.save(stale)

    assert path.read_bytes() == before
    assert journal.load(first.operation_id).note_milestone("execution", "execution-1", "first_layer") is False


def test_update_changes_the_record_on_disk_not_a_snapshot(tmp_path):
    one, two = Journal(str(tmp_path), NS), Journal(str(tmp_path), NS)
    record = one.create("part.3mf", "c" * 64)
    noted = []

    one.update(record.operation_id, lambda r: noted.append(r.note_milestone("job", "j1", "queued")))
    two.update(record.operation_id, lambda r: noted.append(r.note_milestone("job", "j1", "queued")))
    two.update(record.operation_id, lambda r: noted.append(r.note_milestone("execution", "e1", "first_layer")))

    assert noted == [True, False, True]
    final = one.load(record.operation_id)
    assert final.milestones == {"job:j1": ["queued"], "execution:e1": ["first_layer"]}
    assert final.revision == record.revision + 2  # the repeated milestone changed nothing, so nothing was written


def test_update_refuses_a_record_of_a_newer_format(tmp_path):
    journal = Journal(str(tmp_path), NS)
    record = journal.create("part.3mf", "d" * 64)
    path = Path(journal.folder) / f"{record.operation_id}.json"
    path.write_text(json.dumps({**record.as_dict(), "schema": SCHEMA + 1}), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(JournalError, match="newer_schema"):
        journal.update(record.operation_id, lambda r: setattr(r, "phase", "unknown"))

    assert path.read_bytes() == before


def test_a_record_of_the_first_format_is_written_as_the_current_one(tmp_path):
    journal = Journal(str(tmp_path), NS)
    operation_id = "0b8c6a43-5f0e-4ad5-9b8e-0e6d0b0f6c11"
    first_format = {
        "operation_id": operation_id,
        "phase": "uploaded",
        "created_at": 1.0,
        "filename": "a.3mf",
        "source_sha256": "e" * 64,
        "library_file_id": 3,
        "body": None,
        "result": None,
        "error": None,
    }
    (Path(journal.folder) / f"{operation_id}.json").write_text(json.dumps(first_format), encoding="utf-8")

    record = journal.load(operation_id)
    assert record.revision == 0
    record.phase = "unknown"
    journal.save(record)

    stored = json.loads((Path(journal.folder) / f"{operation_id}.json").read_text(encoding="utf-8"))
    assert (stored["schema"], stored["revision"], stored["phase"]) == (SCHEMA, 1, "unknown")


def _first_format(record) -> dict:
    stored = record.as_dict()
    stored.pop("revision")
    stored.pop("schema")
    return stored


def test_a_deleted_record_of_the_first_format_is_not_brought_back(tmp_path):
    journal = Journal(str(tmp_path), NS)
    record = journal.create("part.3mf", "1" * 64)
    path = Path(journal.folder) / f"{record.operation_id}.json"
    path.write_text(json.dumps(_first_format(record)), encoding="utf-8")
    old = journal.load(record.operation_id)
    assert old.revision == 0
    path.unlink()
    old.phase = "unknown"

    with pytest.raises(JournalError, match="missing"):
        journal.save(old)

    assert not path.exists()


def test_a_file_load_rejects_is_never_replaced(tmp_path):
    journal = Journal(str(tmp_path), NS)
    record = journal.create("part.3mf", "2" * 64)
    path = Path(journal.folder) / f"{record.operation_id}.json"
    path.write_text(json.dumps({"schema": SCHEMA, "revision": record.revision}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(TypeError):
        journal.load(record.operation_id)
    record.phase = "unknown"

    with pytest.raises(JournalError, match="damaged"):
        journal.save(record)
    with pytest.raises(JournalError, match="damaged"):
        journal.update(record.operation_id, lambda r: setattr(r, "phase", "unknown"))

    assert path.read_bytes() == before


def test_a_file_naming_another_operation_is_damaged(tmp_path):
    journal = Journal(str(tmp_path), NS)
    one, two = journal.create("a.3mf", "3" * 64), journal.create("b.3mf", "4" * 64)
    path = Path(journal.folder) / f"{one.operation_id}.json"
    path.write_text((Path(journal.folder) / f"{two.operation_id}.json").read_text(encoding="utf-8"), encoding="utf-8")
    before = path.read_bytes()
    one.phase = "unknown"

    with pytest.raises(JournalError, match="damaged"):
        journal.save(one)

    assert path.read_bytes() == before


def test_a_new_record_is_never_written_over_an_existing_file(tmp_path):
    journal = Journal(str(tmp_path), NS)
    stored = journal.create("part.3mf", "5" * 64)
    path = Path(journal.folder) / f"{stored.operation_id}.json"
    before = path.read_bytes()
    fresh = Record(
        operation_id=stored.operation_id, phase="prepared", created_at=2.0, filename="other.3mf", source_sha256="6" * 64
    )

    with pytest.raises(JournalError, match="exists"):
        journal.save(fresh)

    assert path.read_bytes() == before


def test_update_keeps_a_change_inside_a_nested_field(tmp_path):
    journal = Journal(str(tmp_path), NS)
    record = journal.create("part.3mf", "7" * 64, target={"kind": "printer", "printer_id": 1})

    updated = journal.update(record.operation_id, lambda r: r.target.update(printer_id=2))

    assert updated.target["printer_id"] == 2
    stored = journal.load(record.operation_id)
    assert stored.target["printer_id"] == 2 and stored.revision == record.revision + 1


def test_update_keeps_a_scalar_change_and_writes_nothing_for_none(tmp_path):
    journal = Journal(str(tmp_path), NS)
    record = journal.create("part.3mf", "8" * 64)
    path = Path(journal.folder) / f"{record.operation_id}.json"

    changed = journal.update(record.operation_id, lambda r: setattr(r, "phase", "uploading"))
    before = path.read_bytes()
    same = journal.update(record.operation_id, lambda r: setattr(r, "phase", "uploading"))

    assert (changed.phase, changed.revision) == ("uploading", record.revision + 1)
    assert same.revision == changed.revision and path.read_bytes() == before
    same.phase = "uploaded"
    journal.save(same)  # what update returned is the record as stored: a save over it is not stale
    assert journal.load(record.operation_id).phase == "uploaded"


@pytest.mark.parametrize("field", ["operation_id", "revision", "schema"])
def test_update_refuses_a_change_of_what_the_record_is(tmp_path, field):
    journal = Journal(str(tmp_path), NS)
    record = journal.create("part.3mf", "9" * 64)
    path = Path(journal.folder) / f"{record.operation_id}.json"
    before = path.read_bytes()
    other = {"operation_id": "0b8c6a43-5f0e-4ad5-9b8e-0e6d0b0f6c11", "revision": 7, "schema": SCHEMA + 1}

    with pytest.raises(JournalError, match="identity"):
        journal.update(record.operation_id, lambda r: setattr(r, field, other[field]))

    assert path.read_bytes() == before


def _child(script: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(script)], env=env)


def _wait_for(path: Path, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > end:
            raise AssertionError(f"{path.name} never appeared")
        time.sleep(0.02)


def test_another_process_writing_the_record_is_waited_for_and_the_stale_save_refused(tmp_path):
    journal = Journal(str(tmp_path), NS)
    record = journal.create("part.3mf", "f" * 64)
    stale = journal.load(record.operation_id)
    inside, done = tmp_path / "inside", tmp_path / "done"
    child = _child(f"""
        import time
        from pathlib import Path
        from orca_bamdude.journal import Journal

        def mutate(record):
            record.note_milestone("execution", "e1", "first_layer")
            Path({str(inside)!r}).touch()
            time.sleep(1.0)

        Journal({str(tmp_path)!r}, {NS!r}).update({record.operation_id!r}, mutate)
        Path({str(done)!r}).touch()
    """)
    try:
        _wait_for(inside)
        stale.phase = "unknown"
        started = time.monotonic()
        with pytest.raises(JournalError, match="stale"):
            journal.save(stale)
        waited = time.monotonic() - started
    finally:
        assert child.wait(30) == 0
    assert done.exists()
    assert waited > 0.5  # the save waited for the other process's write instead of racing it
    final = journal.load(record.operation_id)
    assert final.milestones == {"execution:e1": ["first_layer"]} and final.phase == "prepared"


def test_a_lock_held_too_long_is_busy_not_a_silent_write(tmp_path):
    journal = Journal(str(tmp_path), NS, lock_timeout=0.3)
    record = journal.create("part.3mf", "0" * 64)
    path = Path(journal.folder) / f"{record.operation_id}.json"
    inside = tmp_path / "inside"
    child = _child(f"""
        import time
        from pathlib import Path
        from orca_bamdude.journal import Journal

        def mutate(record):
            Path({str(inside)!r}).touch()
            time.sleep(2.0)

        Journal({str(tmp_path)!r}, {NS!r}).update({record.operation_id!r}, mutate)
    """)
    try:
        _wait_for(inside)
        before = path.read_bytes()
        record.phase = "unknown"
        with pytest.raises(JournalError, match="busy"):
            journal.save(record)
        assert path.read_bytes() == before
    finally:
        assert child.wait(30) == 0
