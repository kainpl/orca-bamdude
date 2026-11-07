"""Sending a sliced plate (spec r2 §6): the staged copy, the plates, the journal, upload, submit and the answer.

The client is an in-process stand-in: the transport and the connector contract have their own tests, so these run in
milliseconds and show exactly what the operation sent, in which order.
"""

import threading
import time
import zipfile

import pytest

from orca_bamdude.api import Capabilities
from orca_bamdude.journal import Journal
from orca_bamdude.submit import (
    Choice,
    OptionError,
    Request,
    Target,
    UploadGate,
    options_from_params,
    send,
)
from orca_bamdude.transport import HttpError, TransportError

PRINTER = Target.printer(7, "11111111-1111-4111-8111-111111111111")
CAPS = Capabilities(
    server_id="srv",
    storage_epoch="ep",
    actor_id="key",
    server_version="0.8.0",
    enabled=True,
    features={"library_upload": True, "submissions": True, "auto_target": True},
    limits={"max_upload_bytes": 10_000_000, "max_entries": 64, "max_copies_per_entry": 999, "max_total_jobs": 999},
)


def _project(tmp_path, plates=(1,), name="part.gcode.3mf"):
    blocks = "".join(f'<plate><metadata key="index" value="{i}"/></plate>' for i in plates)
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for index in plates:
            archive.writestr(f"Metadata/plate_{index}.gcode", "G28\n" * 10)
        archive.writestr("Metadata/slice_info.config", f"<config>{blocks}</config>")
    return str(path)


class Client:
    """Scripted answers; ``calls`` is what was sent, in order."""

    def __init__(self, *, upload=None, submit=None, gets=()):
        self.calls = []
        self._upload = upload
        self._submit = submit
        self._gets = list(gets)
        self.during_upload = None

    def upload(self, file_path, filename, size, *, progress=None, cancelled=None):
        with open(file_path, "rb") as handle:
            import hashlib

            digest = hashlib.sha256(handle.read()).hexdigest()
        self.calls.append(("upload", filename, size))
        if self.during_upload is not None:
            self.during_upload()
        if cancelled is not None and cancelled():
            raise TransportError("cancelled")
        if progress is not None:
            progress(size, size)
        if isinstance(self._upload, Exception):
            raise self._upload
        return self._upload or {"id": 41, "file_hash": digest}

    def submit(self, body):
        self.calls.append(("submit", body))
        if isinstance(self._submit, Exception):
            raise self._submit
        if self._submit is not None:
            return self._submit(body)
        return 201, {"operation_id": body["operation_id"], "state": "complete", "entries": []}

    def get(self, operation_id, server_id, epoch):
        self.calls.append(("get", operation_id))
        answer = self._gets.pop(0) if self._gets else (202, {"operation_id": operation_id, "state": "processing"})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def cancel(self, operation_id, server_id, epoch):
        self.calls.append(("cancel", operation_id))
        return 200, {"operation_id": operation_id, "state": "cancelled", "entries": []}


@pytest.fixture
def env(tmp_path):
    class Env:
        journal = Journal(str(tmp_path / "data"), CAPS.namespace)
        staging = str(tmp_path / "staging")
        gate = UploadGate()

        def run(self, request, client, *, choose=None, cancel=None, caps=CAPS, writable=True, deadline=1.0):
            return send(
                request,
                client=client,
                capabilities=caps,
                journal=self.journal,
                staging_dir=self.staging,
                gate=self.gate,
                cancel=cancel or threading.Event(),
                choose=choose or (lambda offer: Choice("queue_asap", offer.proposed)),
                host_writable=writable,
                answer_deadline=deadline,
                poll_seconds=0.01,
            )

    return Env()


def _print(tmp_path, plates=(2,), hint=2, **kwargs):
    return Request.native_print(_project(tmp_path, plates), "Cube_plate_2", PRINTER, plate_index=hint, **kwargs)


# -- the happy path and what it sent -----------------------------------------------------------------------------------


def test_a_native_print_uploads_the_staged_copy_then_submits_that_plate(tmp_path, env):
    client = Client()
    outcome = env.run(_print(tmp_path), client)

    assert outcome.result == "accepted"
    assert [c[0] for c in client.calls] == ["upload", "submit"]
    body = client.calls[1][1]
    record = env.journal.load(body["operation_id"])
    assert body["protocol_version"] == 1 and body["library_file_id"] == 41
    assert (body["server_id"], body["storage_epoch"]) == ("srv", "ep")
    assert body["source_sha256"] == record.source_sha256
    (entry,) = body["entries"]
    assert entry["plate_id"] == 2 and entry["action"] == "queue_asap" and entry["quantity"] == 1
    assert entry["target"] == {"kind": "printer", "printer_id": 7, "printer_identity": PRINTER.identity}
    assert entry["filament"] == {"mode": "auto"}
    assert record.phase == "accepted" and record.library_file_id == 41 and record.body == body


def test_the_operation_is_written_down_before_its_first_request(tmp_path, env):
    client = Client()
    seen = []
    client.during_upload = lambda: seen.extend(r.phase for r in env.journal.records())
    env.run(_print(tmp_path), client)
    assert seen == ["uploading"]


def test_library_only_uploads_and_queues_nothing(tmp_path, env):
    client = Client()
    outcome = env.run(_print(tmp_path), client, choose=lambda offer: Choice("library_only", ()))
    assert outcome.result == "accepted" and outcome.code == "library"
    assert [c[0] for c in client.calls] == ["upload"]
    (record,) = env.journal.records()
    assert record.phase == "upload_only" and record.library_file_id == 41


# -- plates (§6.2 step 6) ----------------------------------------------------------------------------------------------


def test_one_sliced_plate_must_be_the_one_orca_named(tmp_path, env):
    client = Client()
    outcome = env.run(_print(tmp_path, plates=(1,), hint=2), client)
    assert outcome.result == "refused" and outcome.code == "plate_ambiguous"
    assert client.calls == [] and env.journal.records() == []


def test_several_sliced_plates_are_offered_together(tmp_path, env):
    offers = []
    env.run(_print(tmp_path, plates=(1, 2, 3), hint=2), Client(), choose=lambda o: offers.append(o) or None)
    assert offers[0].plates == (1, 2, 3) and offers[0].proposed == (1, 2, 3)


def test_a_send_of_all_plates_proposes_none_for_the_queue(tmp_path, env):
    offers = []
    request = Request.native_send(_project(tmp_path, (1, 2)), "Cube.gcode.3mf", PRINTER, plate_index=0)
    env.run(request, Client(), choose=lambda o: offers.append(o) or None)
    assert offers[0].plates == (1, 2) and offers[0].proposed == () and offers[0].default_action == "library_only"


def test_a_queue_action_needs_at_least_one_plate(tmp_path, env):
    client = Client()
    request = Request.native_send(_project(tmp_path, (1, 2)), "Cube.gcode.3mf", PRINTER, plate_index=0)
    outcome = env.run(request, client, choose=lambda o: Choice("queue_manual", ()))
    assert outcome.result == "refused" and outcome.code == "no_plates" and client.calls == []


def test_a_plate_the_file_does_not_hold_is_refused(tmp_path, env):
    client = Client()
    outcome = env.run(_print(tmp_path), client, choose=lambda o: Choice("queue_asap", (9,)))
    assert outcome.result == "refused" and outcome.code == "no_plates" and client.calls == []


# -- the gate before any I/O (§6.2 step 2, §10) ---------------------------------------------------------------------


def test_an_unsupported_host_sends_nothing(tmp_path, env):
    client = Client()
    outcome = env.run(_print(tmp_path), client, writable=False)
    assert outcome.result == "refused" and outcome.code == "host_version"
    assert client.calls == [] and env.journal.records() == []


def test_a_queue_action_the_key_may_not_take_is_refused_before_the_upload(tmp_path, env):
    caps = Capabilities(**{**CAPS.__dict__, "features": {"library_upload": True, "submissions": False}})
    client = Client()
    outcome = env.run(_print(tmp_path), client, caps=caps)
    assert outcome.result == "refused" and outcome.code == "not_permitted" and client.calls == []


def test_a_disabled_connector_sends_nothing(tmp_path, env):
    caps = Capabilities(**{**CAPS.__dict__, "enabled": False})
    client = Client()
    outcome = env.run(_print(tmp_path), client, caps=caps)
    assert outcome.result == "refused" and outcome.code == "connector_disabled" and client.calls == []


def test_the_autoqueue_takes_no_nozzle_offset_choice(tmp_path, env):
    request = Request.manual(_project(tmp_path, (1,)), "part.gcode.3mf", Target.auto("X1C"))
    client = Client()
    choice = Choice("queue_asap", (1,), options={"nozzle_offset_cali": "on"})
    outcome = env.run(request, client, choose=lambda o: choice)
    assert outcome.result == "refused" and outcome.code == "option_not_for_autoqueue" and client.calls == []


# -- the server's answer (§6.2 steps 9-11) --------------------------------------------------------------------------


def test_an_upload_whose_bytes_the_server_hashed_otherwise_is_not_submitted(tmp_path, env):
    client = Client(upload={"id": 41, "file_hash": "0" * 64})
    outcome = env.run(_print(tmp_path), client)
    assert outcome.result == "refused" and outcome.code == "hash_mismatch"
    assert [c[0] for c in client.calls] == ["upload"]


def test_a_refused_upload_is_the_servers_sentence(tmp_path, env):
    client = Client(upload=HttpError(413, "upload_too_large", "Too large"))
    outcome = env.run(_print(tmp_path), client)
    assert outcome.result == "refused" and outcome.code == "upload_too_large" and outcome.message == "Too large"
    (record,) = env.journal.records()
    assert record.phase == "finished" and record.error == "upload_too_large"


def test_work_still_running_is_asked_about_until_it_ends(tmp_path, env):
    done = (200, {"operation_id": "x", "state": "complete", "entries": []})
    client = Client(submit=lambda body: (202, {"state": "processing"}), gets=[(202, {"state": "processing"}), done])
    outcome = env.run(_print(tmp_path), client)
    assert outcome.result == "accepted"
    assert [c[0] for c in client.calls] == ["upload", "submit", "get", "get"]


def test_work_still_running_at_the_deadline_is_unknown_not_failed(tmp_path, env):
    client = Client(submit=lambda body: (202, {"state": "processing"}))
    outcome = env.run(_print(tmp_path), client, deadline=0.05)
    assert outcome.result == "unknown"
    (record,) = env.journal.records()
    assert record.phase == "unknown"


def test_a_submit_whose_answer_was_lost_is_unknown(tmp_path, env):
    client = Client(submit=TransportError("timeout"))
    outcome = env.run(_print(tmp_path), client)
    assert outcome.result == "unknown"
    (record,) = env.journal.records()
    assert record.phase == "unknown" and record.body is not None


def test_a_refused_submit_keeps_the_file_in_the_library(tmp_path, env):
    client = Client(submit=HttpError(409, "context_changed", "Another context"))
    outcome = env.run(_print(tmp_path), client)
    assert outcome.result == "refused" and outcome.code == "context_changed"
    (record,) = env.journal.records()
    assert record.phase == "finished" and record.library_file_id == 41 and record.error == "context_changed"


def test_a_partial_answer_says_so(tmp_path, env):
    partial = (201, {"state": "partial", "entries": [{"entry_id": "e", "state": "rejected"}]})
    outcome = env.run(_print(tmp_path), Client(submit=lambda body: partial))
    assert outcome.result == "accepted" and outcome.code == "partial"


# -- cancellation (§6.2, RF-6) ----------------------------------------------------------------------------------------


def test_a_closed_window_sends_nothing(tmp_path, env):
    client = Client()
    outcome = env.run(_print(tmp_path), client, choose=lambda o: None)
    assert outcome.result == "cancelled" and client.calls == [] and env.journal.records() == []


def test_a_cancel_during_the_upload_queues_nothing(tmp_path, env):
    cancel = threading.Event()
    client = Client()
    client.during_upload = cancel.set
    outcome = env.run(_print(tmp_path), client, cancel=cancel)
    assert outcome.result == "cancelled" and [c[0] for c in client.calls] == ["upload"]
    (record,) = env.journal.records()
    assert record.phase == "finished" and record.error == "cancelled"


def test_a_cancel_after_the_submit_went_out_asks_the_server_to_stop(tmp_path, env):
    cancel = threading.Event()

    def submitted(body):
        cancel.set()
        return 202, {"state": "processing"}

    client = Client(submit=submitted)
    outcome = env.run(_print(tmp_path), client, cancel=cancel)
    assert outcome.result == "cancelled" and [c[0] for c in client.calls] == ["upload", "submit", "cancel"]


def test_the_staged_copy_is_removed_when_the_operation_ends(tmp_path, env):
    import os

    env.run(_print(tmp_path), Client())
    assert [n for n in os.listdir(env.staging) if not n.startswith(".")] == []


# -- one upload at a time (§6.2 step 3) ------------------------------------------------------------------------------


def test_a_second_upload_waits_for_the_first():
    gate = UploadGate()
    order = []
    with gate.slot(threading.Event(), deadline=1.0):
        worker = threading.Thread(target=lambda: order.append(gate.slot(threading.Event(), 1.0).__enter__()))
        worker.start()
        time.sleep(0.1)
        assert order == []
    worker.join(2)
    assert order == [True]


def test_a_second_upload_gives_up_at_its_deadline():
    gate = UploadGate()
    with (
        gate.slot(threading.Event(), deadline=1.0),
        pytest.raises(TimeoutError),
        gate.slot(threading.Event(), deadline=0.05),
    ):
        pass


# -- options from Orca's print dialog (§6.3; rc SelectMachine PrintOption::getValueInt) -------------------------------


class Params:
    auto_bed_leveling = 1
    auto_flow_cali = 2
    auto_offset_cali = 0
    task_vibration_cali = False
    task_record_timelapse = True
    task_layer_inspect = False


def test_orcas_option_codes_read_as_off_on_auto():
    options, warnings = options_from_params(Params())
    assert options == {
        "bed_levelling": "on",
        "flow_cali": "auto",
        "nozzle_offset_cali": "off",
        "timelapse": True,
        "layer_inspect": False,
    }
    assert warnings == ()


def test_an_unknown_option_code_is_refused_by_name():
    params = Params()
    params.auto_flow_cali = 3
    with pytest.raises(OptionError) as refused:
        options_from_params(params)
    assert refused.value.option == "flow_cali"


def test_vibration_calibration_is_a_named_warning():
    params = Params()
    params.task_vibration_cali = True
    _options, warnings = options_from_params(params)
    assert warnings == ("vibration_cali_unsupported",)
