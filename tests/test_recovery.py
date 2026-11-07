"""What the history does with a record (spec r2 §7): reconcile by GET, resume under the same id, cancel what is not
published, send as new only when confirmed -- and never anything for another namespace's record."""

import uuid

import pytest

from orca_bamdude.api import Capabilities
from orca_bamdude.journal import Journal
from orca_bamdude.recovery import actions_for, cancel, reconcile, reconcile_pending, resume, send_as_new
from orca_bamdude.transport import HttpError, TransportError

CAPS = Capabilities("srv", "ep", "key", "0.8.0", True, {"submissions": True}, {})


class Client:
    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def _answer(self, name, *args):
        self.calls.append((name, *args))
        answer = self.answers.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else (200, {"state": "complete", "entries": []})

    def get(self, operation_id, server_id, epoch):
        return self._answer("get", operation_id)

    def resume(self, operation_id, server_id, epoch):
        return self._answer("resume", operation_id)

    def cancel(self, operation_id, server_id, epoch):
        return self._answer("cancel", operation_id)

    def submit(self, body):
        return self._answer("submit", body)


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path), CAPS.namespace)


def _record(journal, phase, *, body=True, result=None):
    record = journal.create("part.gcode.3mf", "a" * 64, origin="native", plates=[2], target={"kind": "printer"})
    payload = {
        "protocol_version": 1,
        "operation_id": record.operation_id,
        "server_id": "srv",
        "storage_epoch": "ep",
        "library_file_id": 41,
        "source_sha256": "a" * 64,
        "entries": [{"entry_id": str(uuid.uuid4()), "plate_id": 2, "quantity": 1, "action": "queue_asap"}],
    }

    def mutate(stored):
        stored.phase, stored.library_file_id = phase, 41
        stored.body = payload if body else None
        stored.result = result

    return journal.update(record.operation_id, mutate)


# -- reconcile: GET only ----------------------------------------------------------------------------------------------


def test_an_unknown_send_the_server_finished_is_recorded_as_accepted(journal):
    record = _record(journal, "unknown")
    client = Client()
    outcome = reconcile(record, client, CAPS, journal)
    assert outcome.result == "accepted" and [c[0] for c in client.calls] == ["get"]
    assert journal.load(record.operation_id).phase == "accepted"


def test_a_submit_the_server_never_received_can_be_sent_again_under_its_id(journal):
    record = _record(journal, "unknown")
    client = Client({"get": HttpError(404, "operation_not_found", "")})
    outcome = reconcile(record, client, CAPS, journal)
    assert outcome.result == "refused" and outcome.code == "not_received"
    stored = journal.load(record.operation_id)
    assert stored.phase == "uploaded" and stored.body is not None
    assert "resume" in actions_for(stored, journal)


@pytest.mark.parametrize(
    "error,code",
    [
        (HttpError(409, "context_changed", ""), "context_changed"),
        (HttpError(410, "receipt_compacted", ""), "receipt_compacted"),
    ],
)
def test_another_context_and_a_compacted_receipt_are_final(journal, error, code):
    record = _record(journal, "unknown")
    outcome = reconcile(record, Client({"get": error}), CAPS, journal)
    assert outcome.code == code
    stored = journal.load(record.operation_id)
    assert stored.phase == "finished" and stored.error == code and actions_for(stored, journal) == ()


def test_a_server_that_cannot_be_asked_now_leaves_the_record_as_it_was(journal):
    record = _record(journal, "unknown")
    outcome = reconcile(record, Client({"get": TransportError("timeout")}), CAPS, journal)
    assert outcome.result == "unknown" and journal.load(record.operation_id).phase == "unknown"


def test_after_a_restart_only_gets_are_sent(journal):
    _record(journal, "unknown")
    _record(journal, "submitting")
    _record(journal, "finished")
    client = Client()
    outcomes = reconcile_pending(journal, client, CAPS)
    assert len(outcomes) == 2 and {c[0] for c in client.calls} == {"get"}


# -- resume, cancel, send as new ----------------------------------------------------------------------------------


def test_resume_resends_a_body_the_server_never_received_under_the_same_id(journal):
    record = _record(journal, "uploaded")
    client = Client({"submit": (201, {"state": "complete", "entries": []})})
    outcome = resume(record, client, CAPS, journal)
    assert outcome.result == "accepted"
    (call,) = client.calls
    assert call[0] == "submit" and call[1]["operation_id"] == record.operation_id


def test_resume_asks_the_server_to_run_what_it_has_not_published(journal):
    partial = {"state": "partial", "entries": [{"entry_id": "e", "state": "retryable_failure"}]}
    record = _record(journal, "accepted", result=partial)
    assert "resume" in actions_for(record, journal) and "cancel" in actions_for(record, journal)
    client = Client()
    assert resume(record, client, CAPS, journal).result == "accepted"
    assert client.calls == [("resume", record.operation_id)]


def test_cancel_fences_only_what_is_unpublished(journal):
    record = _record(journal, "accepted", result={"state": "processing", "entries": []})
    client = Client({"cancel": (200, {"state": "cancelled", "entries": []})})
    assert cancel(record, client, CAPS, journal).result == "cancelled"
    assert journal.load(record.operation_id).phase == "finished"


def test_send_as_new_needs_an_explicit_confirmation(journal):
    record = _record(journal, "finished")
    client = Client()
    assert send_as_new(record, client, CAPS, journal, confirmed=False).code == "confirm"
    assert client.calls == []


def test_send_as_new_is_a_new_operation_with_the_same_file(journal):
    record = _record(journal, "finished")
    client = Client({"submit": (201, {"state": "complete", "entries": []})})
    outcome = send_as_new(record, client, CAPS, journal, confirmed=True)
    assert outcome.result == "accepted" and outcome.operation_id != record.operation_id
    body = client.calls[0][1]
    assert body["operation_id"] == outcome.operation_id and body["library_file_id"] == 41
    assert body["entries"][0]["entry_id"] != record.body["entries"][0]["entry_id"]
    assert journal.load(outcome.operation_id).phase == "accepted"


def test_another_namespaces_record_is_never_sent_again(tmp_path, journal):
    other = Journal(str(tmp_path), ("srv", "old-epoch", "key"))
    record = _record(other, "unknown")
    client = Client()
    assert actions_for(record, journal) == ()
    for action in (resume, cancel):
        assert action(record, client, CAPS, journal).code == "foreign_namespace"
    assert send_as_new(record, client, CAPS, journal, confirmed=True).code == "foreign_namespace"
    assert client.calls == []


def test_a_gateway_error_on_a_resend_is_unknown_not_final(journal):
    record = _record(journal, "uploaded")
    outcome = resume(record, Client({"submit": HttpError(502, "http_error", "")}), CAPS, journal)
    assert outcome.result == "unknown" and journal.load(record.operation_id).phase == "unknown"


def test_a_full_disk_while_recording_a_reconcile_keeps_its_answer(journal, monkeypatch):
    record = _record(journal, "unknown")

    def full(operation_id, mutate):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(journal, "update", full)
    assert reconcile(record, Client(), CAPS, journal).result == "accepted"
