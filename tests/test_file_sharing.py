"""Files two threads or two OrcaSlicer processes share: a reader and an atomic replace must not break each other.

On Windows a file another handle has open cannot be replaced (``os.replace`` raises PermissionError, WinError 5),
and opening a file while it is being replaced can fail the same way. Measured on this machine: 20 000 reads beside a
writer replacing ``state.json`` gave a reader PermissionError and a writer PermissionError (H2 probe, 2026-11-06).
A sharing violation is waited out for a bounded time; one that persists is still the error it was.
"""

import json
import os
import sys
import threading

import pytest

from orca_bamdude import journal, locking, permits, staging


class Flaky:
    """Raises PermissionError the first ``times`` calls, then behaves as ``real``."""

    def __init__(self, real, times):
        self.real, self.times, self.calls = real, times, 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.times:
            raise PermissionError(13, "The process cannot access the file")
        return self.real(*args, **kwargs)


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(locking.time, "sleep", slept.append)
    return slept


def test_a_replace_waits_out_a_reader(tmp_path, monkeypatch, no_sleep):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("{}", encoding="utf-8")
    flaky = Flaky(os.replace, 3)
    monkeypatch.setattr(locking.os, "replace", flaky)
    locking.replace(str(src), str(dst))
    assert flaky.calls == 4 and dst.read_text(encoding="utf-8") == "{}"
    assert len(no_sleep) == 3


def test_a_replace_that_keeps_failing_is_the_error_it_was(tmp_path, monkeypatch, no_sleep):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(locking.os, "replace", Flaky(os.replace, 10**6))
    with pytest.raises(PermissionError):
        locking.replace(str(src), str(dst))
    assert sum(no_sleep) < 1.5  # bounded


def test_a_read_waits_out_a_replace(tmp_path, monkeypatch, no_sleep):
    path = tmp_path / "state.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    flaky = Flaky(open, 2)
    monkeypatch.setattr(locking, "open", flaky, raising=False)
    assert locking.read_text(str(path)) == '{"a": 1}'
    assert flaky.calls == 3


def test_a_read_that_keeps_failing_is_the_error_it_was(tmp_path, monkeypatch, no_sleep):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(locking, "open", Flaky(open, 10**6), raising=False)
    with pytest.raises(PermissionError):
        locking.read_text(str(path))
    assert sum(no_sleep) < 0.2  # the UI thread reads the state: a short bound


def test_a_missing_file_is_not_waited_for(tmp_path, no_sleep):
    with pytest.raises(FileNotFoundError):
        locking.read_text(str(tmp_path / "nothing.json"))
    assert no_sleep == []


def test_the_state_read_over_a_passing_sharing_violation_is_the_state(tmp_path, monkeypatch, no_sleep):
    permits.update_state(str(tmp_path), lambda s: {**s, "held_targets": ["network:https://farm.example"]})
    monkeypatch.setattr(locking, "open", Flaky(open, 1), raising=False)
    assert permits.load_state(str(tmp_path))["held_targets"] == ["network:https://farm.example"]


def test_a_state_write_over_a_passing_sharing_violation_is_recorded(tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(locking.os, "replace", Flaky(os.replace, 2))
    permits.update_state(str(tmp_path), lambda s: {**s, "n": 1})
    assert permits.load_state(str(tmp_path))["n"] == 1
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_a_journal_record_read_over_a_passing_sharing_violation(tmp_path, monkeypatch, no_sleep):
    book = journal.Journal(str(tmp_path), ("server", "epoch", "actor"))
    record = book.create("model.3mf", "a" * 64)
    monkeypatch.setattr(locking, "open", Flaky(open, 1), raising=False)
    assert book.load(record.operation_id).operation_id == record.operation_id
    monkeypatch.setattr(locking, "open", Flaky(open, 1), raising=False)
    assert [r.operation_id for r in book.records()] == [record.operation_id]


def test_a_journal_write_over_a_passing_sharing_violation(tmp_path, monkeypatch, no_sleep):
    book = journal.Journal(str(tmp_path), ("server", "epoch", "actor"))
    monkeypatch.setattr(locking.os, "replace", Flaky(os.replace, 2))
    record = book.create("model.3mf", "b" * 64)
    assert book.load(record.operation_id) is not None


def test_a_staged_copy_over_a_passing_sharing_violation(tmp_path, monkeypatch, no_sleep):
    source = tmp_path / "plate.3mf"
    source.write_bytes(b"PK" + b"x" * 64)
    flaky = Flaky(os.replace, 2)
    monkeypatch.setattr(locking.os, "replace", flaky)
    staged = staging.stage(str(source), str(tmp_path / "staging"), 1024)
    assert flaky.calls == 3 and os.path.exists(staged.path)


@pytest.mark.skipif(sys.platform != "win32", reason="the sharing violation is Windows'")
def test_a_reader_and_a_writer_side_by_side_never_see_an_unreadable_state(tmp_path):
    folder = str(tmp_path)
    permits.update_state(folder, lambda s: {**s, "n": 0})
    stop, failures = threading.Event(), []

    def write():
        n = 0
        while not stop.is_set():
            n += 1
            try:
                permits.update_state(folder, lambda s, n=n: {**s, "n": n})
            except Exception as exc:  # noqa: BLE001
                failures.append(repr(exc))

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    unreadable = sum(permits.load_state(folder) is None for _ in range(5000))
    stop.set()
    writer.join(10)
    assert unreadable == 0 and failures == []
    assert json.loads((tmp_path / permits.STATE_FILE).read_text(encoding="utf-8"))["n"] > 0
