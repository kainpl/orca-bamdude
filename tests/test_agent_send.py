"""Orca's Print and Send end to end on the fake OrcaSlicer, against a scripted BamDude on 127.0.0.1 (spec r2 §6;
plan r2 H3): the window, the upload and the submit, a Cancel while the window is open, a device of another
connection, and the notification that says where the file went."""

import hashlib
import json
import sys
import threading
import time
import zipfile
from urllib.parse import urlsplit

import pytest
from fake_orca import Host
from fakes import json_answer
from test_agent import AGENT, IDENTITY, Farm, connected, wait_for


class SendFarm(Farm):
    """The agent tests' farm, with a key that may upload and submit."""

    def __init__(self):
        super().__init__()
        self.submit_answer = None
        self.ams = None

    def route(self, method, path):
        parts = urlsplit(path)
        if parts.path == "/base/api/v1/connector/capabilities":
            status, headers, payload = super().route(method, path)
            body = json.loads(payload)
            body["features"].update(library_upload=True, submissions=True)
            body["limits"].update(max_upload_bytes=50_000_000, max_entries=64, max_copies_per_entry=999)
            return json_answer(200, body)
        if parts.path == "/base/api/v1/printers/status/batch" and self.ams is not None:
            status, headers, payload = super().route(method, path)
            body = json.loads(payload)
            for entry in body.values():
                entry["ams"] = self.ams
            return json_answer(200, body)
        if parts.path == "/base/api/v1/connector/files" and method == "POST":
            received = self.server.seen[-1]["body"]
            return json_answer(201, {"id": 41, "file_hash": hashlib.sha256(received).hexdigest()})
        if parts.path == "/base/api/v1/connector/submissions" and method == "POST":
            sent = json.loads(self.server.seen[-1]["body"])
            return self.submit_answer or json_answer(
                201, {"operation_id": sent["operation_id"], "state": "complete", "entries": []}
            )
        return super().route(method, path)


@pytest.fixture
def farm():
    farm = SendFarm()
    yield farm
    farm.server.close()


@pytest.fixture
def host(tmp_path, farm):
    host = Host(tmp_path / "data")
    host.install()
    host.write_base(AGENT, {"server_url": farm.server.url + "/base", "api_key": "bd_test_key"})
    yield host
    host.close()


def _project(tmp_path, plates):
    path = tmp_path / "orca-temp.3mf"
    blocks = "".join(f'<plate><metadata key="index" value="{i}"/></plate>' for i in plates)
    with zipfile.ZipFile(path, "w") as archive:
        for index in plates:
            archive.writestr(f"Metadata/plate_{index}.gcode", "G28\n" * 10)
        archive.writestr("Metadata/slice_info.config", f"<config>{blocks}</config>")
    return str(path)


class Params:
    """``PrintParams`` as P0 recorded it for a LAN Print (rc PrintJob::process)."""

    def __init__(self, dev_id, filename, plate_index, project_name):
        self.dev_id, self.filename, self.plate_index, self.project_name = dev_id, filename, plate_index, project_name
        self.auto_bed_leveling, self.auto_flow_cali, self.auto_offset_cali = 1, 1, 2
        self.task_vibration_cali, self.task_record_timelapse, self.task_layer_inspect = False, False, True
        self.connection_type = "lan"


def _job(host, method, params, cancel=lambda: False):
    result, updates = {}, []

    extra = () if method == "start_local_print" else (None,)  # Send also takes wait_fn (None for a LAN send, P0)

    def run():
        result["code"] = host.job_call(method, params, lambda *a: updates.append(a), cancel, *extra)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result, updates


def _notified(host, words):
    return wait_for(lambda: any(words in n["text"] for n in host.notifications))


def test_a_print_from_orca_reaches_the_farms_queue(host, farm, tmp_path):
    dev = connected(host)
    params = Params(dev, _project(tmp_path, (2,)), 2, "Cube_plate_2")
    thread, result, updates = _job(host, "start_local_print", params)
    assert wait_for(lambda: host.windows and host.windows[0]["open"])
    assert host.windows[0]["style"] == 1  # modal
    host.window_submit(0, {"action": "queue_asap", "plates": [2], "copies": 1, "filament": "auto"})
    thread.join(10)
    assert result["code"] == 0
    (sent,) = farm.requests("/connector/submissions")
    body = json.loads(sent["body"])
    (entry,) = body["entries"]
    assert entry["plate_id"] == 2 and entry["target"] == {
        "kind": "printer",
        "printer_id": 1,
        "printer_identity": IDENTITY,
    }
    assert entry["options"]["bed_levelling"] == "on" and entry["options"]["nozzle_offset_cali"] == "auto"
    assert body["library_file_id"] == 41
    assert updates and all(update[0] == 1 for update in updates)  # Upload stage only; Finished is never sent
    assert _notified(host, "Cube_plate_2")
    assert "X1C left" in host.notifications[-1]["text"]


def test_a_cancel_while_the_window_is_open_returns_at_once_and_sends_nothing(host, farm, tmp_path):
    dev = connected(host)
    pressed = threading.Event()
    params = Params(dev, _project(tmp_path, (2,)), 2, "Cube_plate_2")
    thread, result, _ = _job(host, "start_local_print", params, cancel=pressed.is_set)
    assert wait_for(lambda: host.windows and host.windows[0]["open"])
    started = time.monotonic()
    pressed.set()
    thread.join(3)
    assert result["code"] == -18 and time.monotonic() - started < 1.0
    assert wait_for(lambda: host.windows[0]["closed_by_plugin"])
    time.sleep(0.2)
    assert farm.requests("/connector/files") == [] and farm.requests("/connector/submissions") == []


def test_a_device_of_another_connection_is_refused_with_a_warning(host, farm, tmp_path):
    connected(host)
    params = Params("bamdude-someone-else", _project(tmp_path, (1,)), 1, "Cube_plate_1")
    thread, result, _ = _job(host, "start_local_print", params)
    thread.join(5)
    assert result["code"] == -1 and host.windows == []
    assert _notified(host, "Cube_plate_1")
    assert host.notifications[-1]["level"].name == "WarningNotificationLevel"
    assert farm.requests("/connector/files") == []


def test_a_send_of_all_plates_goes_to_the_library_unless_a_queue_is_chosen(host, farm, tmp_path):
    dev = connected(host)
    params = Params(dev, _project(tmp_path, (1, 2)), 0, "Cube.gcode.3mf")
    thread, result, _ = _job(host, "start_send_gcode_to_sdcard", params)
    assert wait_for(lambda: host.windows and host.windows[0]["open"])
    html = host.windows[0]["html"]
    assert 'value="library_only" checked' in html and 'data-plate="1" checked' not in html
    host.window_submit(0, {"action": "library_only", "plates": [], "copies": 1, "filament": "auto"})
    thread.join(10)
    assert result["code"] == 0
    assert len(farm.requests("/connector/files")) == 1 and farm.requests("/connector/submissions") == []
    assert _notified(host, "library")


def test_the_slots_chosen_in_orca_can_be_kept(host, farm, tmp_path):
    farm.ams = [{"id": 0, "tray": [{"id": 2, "tray_type": "PLA"}]}]
    dev = connected(host)
    assert wait_for(lambda: host.device_messages and "ams" in host.device_messages[-1][1])
    params = Params(dev, _project(tmp_path, (2,)), 2, "Cube_plate_2")
    params.ams_mapping2 = '[{"ams_id":0,"slot_id":2},{"ams_id":255,"slot_id":255}]'
    thread, result, _ = _job(host, "start_local_print", params)
    assert wait_for(lambda: host.windows and host.windows[0]["open"])
    assert 'value="pinned"' in host.windows[0]["html"]
    host.window_submit(0, {"action": "queue_asap", "plates": [2], "copies": 1, "filament": "pinned"})
    thread.join(10)
    assert result["code"] == 0
    (sent,) = farm.requests("/connector/submissions")
    assert json.loads(sent["body"])["entries"][0]["filament"] == {"mode": "pinned", "ams_mapping": [2, -1]}


def test_slots_the_printer_does_not_show_are_not_kept_and_the_window_says_so(host, farm, tmp_path):
    dev = connected(host)
    params = Params(dev, _project(tmp_path, (2,)), 2, "Cube_plate_2")
    params.ams_mapping2 = '[{"ams_id":3,"slot_id":1}]'
    thread, result, _ = _job(host, "start_local_print", params)
    assert wait_for(lambda: host.windows and host.windows[0]["open"])
    html = host.windows[0]["html"]
    assert 'value="pinned"' not in html and 'class="warn"' in html
    host.window_close(0)
    thread.join(10)
    assert result["code"] == -18  # a closed window is a cancel: nothing was sent
    assert farm.requests("/connector/files") == []


# -- the tab's send from the export folder (spec r2 §8.4) -------------------------------------------------------------


def test_a_file_of_the_export_folder_is_sent_by_its_token(tmp_path, farm):
    from orca_bamdude.submit import Target

    exports = tmp_path / "exports"
    exports.mkdir()
    with open(_project(tmp_path, (1,)), "rb") as handle:
        (exports / "bracket.gcode.3mf").write_bytes(handle.read())
    host = Host(tmp_path / "data2")
    host.install()
    host.write_base(
        AGENT, {"server_url": farm.server.url + "/base", "api_key": "bd_test_key", "export_dir": str(exports)}
    )
    try:
        connected(host)
        adapter = sys.modules["orca_bamdude.orca_adapter"]
        (item,) = adapter.list_exports()
        assert item.relative == "bracket.gcode.3mf" and str(exports) not in item.token
        started = host.ui.call(lambda: adapter.manual_send(item.token, Target.printer(1, IDENTITY), "X1C left"))
        assert started is True
        assert wait_for(lambda: host.windows and host.windows[0]["open"])
        assert 'value="library_only" checked' in host.windows[0]["html"]
        host.window_submit(0, {"action": "queue_manual", "plates": [1], "copies": 2, "filament": "auto"})
        assert wait_for(lambda: farm.requests("/connector/submissions"))
        body = json.loads(farm.requests("/connector/submissions")[0]["body"])
        assert body["entries"][0]["quantity"] == 2 and body["entries"][0]["action"] == "queue_manual"
        assert _notified(host, "bracket.gcode.3mf")
        # A token is spent once: the same one sends nothing more.
        host.ui.call(lambda: adapter.manual_send(item.token, Target.printer(1, IDENTITY), "X1C left"))
        assert wait_for(lambda: any("bracket" in n["text"] and n["level"].name == "WarningNotificationLevel"
                                    for n in host.notifications))  # fmt: skip
        assert len(farm.requests("/connector/files")) == 1
    finally:
        host.close()


# -- following the send (spec r2 §9; plan r2 H5) -----------------------------------------------------------------------


def test_a_printed_send_is_followed_until_it_starts(host, farm, tmp_path):
    observed = {"jobs": [{"entry_id": "e1", "job": {"kind": "queue", "identity": "q1"}, "state": "printing",
                          "printer": {"id": 1, "identity": IDENTITY},
                          "executions": [{"identity": "a1", "state": "printing", "first_layer": None}],
                          "executions_complete": True}], "next_cursor": None}  # fmt: skip
    route = farm.route

    def with_observations(method, path):
        parts = urlsplit(path)
        if method == "GET" and parts.path.startswith("/base/api/v1/connector/submissions/"):
            op = parts.path.rsplit("/", 1)[1]
            return json_answer(200, {"operation_id": op, "state": "complete", "entries": [], "observations": observed})
        return route(method, path)

    farm.server.routes = with_observations
    dev = connected(host)
    params = Params(dev, _project(tmp_path, (2,)), 2, "Cube_plate_2")
    thread, result, _ = _job(host, "start_local_print", params)
    assert wait_for(lambda: host.windows and host.windows[0]["open"])
    host.window_submit(0, {"action": "queue_asap", "plates": [2], "copies": 1, "filament": "auto"})
    thread.join(10)
    assert result["code"] == 0
    assert wait_for(
        lambda: any("X1C left" in n["text"] and "Cube_plate_2" in n["text"] for n in host.notifications[1:])
    )
    gets = [s for s in farm.server.seen if s["method"] == "GET" and "observations=1" in s["path"]]
    assert gets
