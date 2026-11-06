"""The printer agent end to end on the fake OrcaSlicer, against a scripted BamDude on 127.0.0.1 (host spec r2.3.2
§5.1-§5.3; plan r2 H2): discovery, connect, reports, refusals in the user's language, the camera address, commands,
and an unload that leaves no thread and no listening port behind."""

import json
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fake_orca import Host
from fakes import Server, json_answer

AGENT = "BamDudePrinter"
IDENTITY = "11111111-1111-4111-8111-111111111111"


def wait_for(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class Farm:
    """A BamDude with one X1C (and one printer of a model it does not know)."""

    def __init__(self):
        self.epoch = "epoch-1"
        self.status_code = 200
        self.server = Server(self.route)

    def route(self, method, path):
        parts = urlsplit(path)
        if parts.path == "/base/api/v1/connector/capabilities":
            return json_answer(
                200,
                {
                    "product": "bamdude",
                    "protocol_version": 1,
                    "connector_server_id": "server-1",
                    "storage_epoch": self.epoch,
                    "actor_id": "actor-1",
                    "server_version": "0.7.3",
                    "enabled": True,
                    "features": {"camera_stream": True, "monitor": True},
                    "limits": {"poll_min_seconds": 3},
                },
            )
        if parts.path == "/base/api/v1/printers/":
            return json_answer(
                200,
                [
                    {"id": 1, "name": "X1C left", "connector_identity": IDENTITY, "model_code": "BL-P001"},
                    {"id": 2, "name": "Mystery", "connector_identity": "other", "model_code": None},
                ],
            )
        if parts.path == "/base/api/v1/printers/status/batch":
            if self.status_code != 200:
                return json_answer(self.status_code, {"detail": "nope"})
            ids = [int(i) for i in parse_qs(parts.query).get("ids", [])]
            status = {
                "id": 1,
                "name": "X1C left",
                "connector_identity": IDENTITY,
                "connected": True,
                "state": "RUNNING",
                "progress": 12.0,
                "temperatures": {"bed": 55.0, "nozzle": 220.0},
                "nozzles": [{"nozzle_type": "hardened_steel", "nozzle_diameter": "0.4"}],
                "firmware_version": "01.08.02.00",
            }
            return json_answer(200, {str(i): status for i in ids if i == 1})
        return json_answer(404, {"detail": {"error": "not_found", "message": ""}})

    def requests(self, suffix):
        return [s for s in self.server.seen if urlsplit(s["path"]).path.endswith(suffix)]


@pytest.fixture
def farm():
    farm = Farm()
    yield farm
    farm.server.close()


@pytest.fixture
def host(tmp_path, farm):
    host = Host(tmp_path / "data")
    host.install()
    host.write_base(AGENT, {"server_url": farm.server.url + "/base", "api_key": "bd_test_key"})
    yield host
    host.close()


def module():
    return sys.modules["orca_bamdude.orca_adapter"]


def our_device(host):
    assert wait_for(lambda: any(d["dev_type"] == "BL-P001" for d in host.devices.values()))
    return next(dev for dev, d in host.devices.items() if d["dev_type"] == "BL-P001")


def reports(host, dev):
    out = []
    for target, message in list(host.device_messages):
        body = json.loads(message)
        if target == dev and "print" in body:
            out.append(body["print"])
    return out


def connected(host):
    assert host.load()
    host.select_agent()
    dev = our_device(host)
    host.connect_device(dev)
    assert wait_for(lambda: (0, dev, "0") in host.connects)
    return dev


def test_selecting_the_agent_announces_the_farms_known_printers(host, farm):
    assert host.load()
    host.select_agent()
    dev = our_device(host)
    announced = host.devices[dev]
    assert announced["dev_name"] == "X1C left" and announced["dev_ip"] == "127.0.0.1"
    assert dev.startswith("bamdude-")
    assert len(host.devices) == 1  # the printer of an unknown model is not offered


def test_connecting_sends_the_version_a_full_report_and_ok(host, farm):
    dev = connected(host)
    assert dev in host.connected_topics
    messages = [json.loads(m) for d, m in host.device_messages if d == dev]
    assert messages[0]["info"]["module"] == [{"name": "ota", "sw_ver": "01.08.02.00"}]
    first = messages[1]["print"]
    assert first["msg"] == 0 and first["gcode_state"] == "RUNNING" and first["mc_percent"] == 12
    assert first["sdcard"] is True and first["support_send_to_sd"] is True


def test_a_connected_device_keeps_getting_reports(host, farm):
    dev = connected(host)
    count = len(reports(host, dev))
    assert wait_for(lambda: len(reports(host, dev)) > count, timeout=5)


def test_a_device_the_farm_does_not_know_is_refused_with_a_sentence(host, farm):
    assert host.load()
    host.select_agent()
    our_device(host)
    host.connect_device("bamdude-0000000000000000000000000000dead")
    assert wait_for(lambda: host.dialogs)
    (dialog,) = host.dialogs
    assert "earlier BamDude connection" in dialog
    assert "code=5]" not in dialog and "code=-1]" not in dialog


def test_the_refusal_speaks_orcas_language(host, farm):
    host.language = "uk_UA"
    assert host.load()
    host.select_agent()
    our_device(host)
    host.connect_device("bamdude-0000000000000000000000000000dead")
    assert wait_for(lambda: host.dialogs)
    assert "попередньому з'єднанню BamDude" in host.dialogs[0]


def test_without_settings_a_connect_says_where_to_set_them_up(tmp_path):
    host = Host(tmp_path / "data")
    host.install()
    try:
        assert host.load()
        host.select_agent()
        host.connect_device("bamdude-anything")
        assert wait_for(lambda: host.dialogs)
        assert "BamDude tab" in host.dialogs[0]
    finally:
        host.close()


def test_report_requests_are_answered_and_other_commands_refused(host, farm):
    dev = connected(host)
    agent = host.capability(AGENT)
    before = len(reports(host, dev))
    assert (
        host.ui.call(lambda: agent.send_message_to_printer(dev, json.dumps({"pushing": {"command": "pushall"}}), 0, 0))
        == 0
    )
    assert wait_for(lambda: len(reports(host, dev)) > before)
    command = json.dumps({"print": {"command": "gcode_line", "param": "G28"}})
    assert host.ui.call(lambda: agent.send_message(dev, command, 0, 0)) == -7010


def test_the_access_code_orca_was_given_is_echoed(host, farm):
    assert host.load()
    host.select_agent()
    dev = our_device(host)
    host.connect_device(dev, "87654321")
    assert wait_for(lambda: (0, dev, "0") in host.connects)
    agent = host.capability(AGENT)
    ask = json.dumps({"system": {"command": "get_access_code"}})
    assert host.ui.call(lambda: agent.send_message_to_printer(dev, ask, 0, 0)) == 0

    def echoed():
        return [json.loads(m) for d, m in host.device_messages if d == dev and "system" in json.loads(m)]

    assert wait_for(echoed)
    assert echoed()[0]["system"]["access_code"] == "87654321"


def test_the_camera_is_the_relays_address_on_loopback(host, farm):
    dev = connected(host)
    agent = host.capability(AGENT)
    assert host.ui.call(agent.get_camera_stream_mode).name == "HTTP"
    url = host.ui.call(agent.get_camera_url)
    parts = urlsplit(url)
    assert parts.scheme == "http" and parts.hostname == "127.0.0.1"
    assert parts.path.endswith("/" + IDENTITY)
    assert "bd_test_key" not in url
    assert dev  # the address is for the connected device only


def test_reads_are_only_what_the_device_needs(host, farm):
    connected(host)
    time.sleep(1.0)
    assert len(farm.requests("/printers/")) == 1  # one list read for discovery; no fleet polling without the tab
    assert farm.requests("/status/batch")


def test_an_unload_leaves_no_thread_no_listener_and_no_further_report(host, farm):
    dev = connected(host)
    agent = host.capability(AGENT)
    url = host.ui.call(agent.get_camera_url)
    port = urlsplit(url).port
    runtime = module().RUNTIME
    host.unload()
    assert runtime.fleet.poller.generation > 0
    assert wait_for(
        lambda: not [t for t in threading.enumerate() if t.name in ("bamdude-fleet", "orca-bamdude-camera-relay")]
    )
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    settled = len(reports(host, dev))
    time.sleep(3.5)
    assert len(reports(host, dev)) == settled


def test_activate_again_brings_a_working_agent_back(host, farm):
    dev = connected(host)
    host.unload()
    host.connects.clear()
    assert host.activate()
    host.select_agent()
    host.connect_device(dev)
    assert wait_for(lambda: (0, dev, "0") in host.connects)


def test_the_fleets_stop_is_a_shutdown_step_of_its_own(host, farm):
    connected(host)
    runtime = module().RUNTIME
    report = runtime.shutdown("test")
    assert report["steps"].get("stop") == "ok"
