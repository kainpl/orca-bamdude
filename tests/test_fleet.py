"""The farm as one connection generation sees it (host spec r2.3.2 §5.1-§5.3, §8.2; plan r2 H2)."""

import json
from types import SimpleNamespace

import pytest

from orca_bamdude.api import Capabilities
from orca_bamdude.camera_relay import Target
from orca_bamdude.devices import Binding, dev_id
from orca_bamdude.fleet import PUSH_SECONDS, STALE_SECONDS, Fleet, HostCallbacks
from orca_bamdude.poller import Poller
from orca_bamdude.service import LoadContext
from orca_bamdude.transport import HttpError, TransportError

BASE = "https://farm.example/bamdude"
IDENTITY = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Jobs:
    def __init__(self):
        self.queued = []

    def __call__(self, job):
        self.queued.append(job)

    def run_all(self):
        while self.queued:
            jobs, self.queued = self.queued, []
            for job in jobs:
                job()


class Server:
    """A BamDude as the client sees it: answers by name, with a call log."""

    def __init__(self):
        self.calls = []
        self.epoch = "epoch-1"
        self.actor = "actor-1"
        self.features = {"camera_stream": True}
        self.printers = [
            {"id": 1, "name": "X1C left", "connector_identity": IDENTITY, "model_code": "BL-P001"},
            {"id": 2, "name": "Unknown model", "connector_identity": OTHER, "model_code": None},
        ]
        self.statuses = {1: self.status()}
        self.fail = {}  # method -> exception to raise once

    @staticmethod
    def status(**overrides):
        status = {
            "id": 1,
            "name": "X1C left",
            "connector_identity": IDENTITY,
            "connected": True,
            "state": "IDLE",
            "temperatures": {"bed": 24.0},
            "firmware_version": "01.08.02.00",
        }
        status.update(overrides)
        return status

    def _maybe_fail(self, name):
        error = self.fail.pop(name, None)
        if error is not None:
            raise error

    def capabilities(self):
        self.calls.append("capabilities")
        self._maybe_fail("capabilities")
        return Capabilities("server-1", self.epoch, self.actor, "0.7.3", True, dict(self.features), {})

    def get_printers(self):
        self.calls.append("printers")
        self._maybe_fail("printers")
        return [dict(p) for p in self.printers]

    def status_batch(self, ids):
        self.calls.append(("status", tuple(ids)))
        self._maybe_fail("status")
        return {pid: dict(self.statuses[pid]) for pid in ids if pid in self.statuses}


class Client:
    def __init__(self, server, base_url):
        self.server, self.base_url = server, base_url

    def capabilities(self):
        return self.server.capabilities()

    def printers(self):
        return self.server.get_printers()

    def status_batch(self, ids):
        return self.server.status_batch(ids)

    def open_camera(self, printer_id, identity):
        self.server.calls.append(("camera", printer_id, identity, self.base_url))
        return "stream"


class Relay:
    def __init__(self):
        self.target = None
        self.rotations = 0
        self.started = self.stopped = False

    def set_target(self, target):
        self.target = target

    def rotate(self):
        self.rotations += 1

    def url_for(self, target):
        return f"http://127.0.0.1:1/{self.rotations}/{target.identity}" if target == self.target else None

    def start(self):
        self.started = True

    def stop(self, timeout=None):
        self.stopped = True
        return True


def snapshot(generation, base=BASE, blocked=None):
    connection = None if blocked else SimpleNamespace(base_url=base)
    return SimpleNamespace(generation=generation, connection=connection, blocked=blocked)


class Rig:
    def __init__(self):
        self.clock, self.jobs, self.server, self.relay = Clock(), Jobs(), Server(), Relay()
        self.context = LoadContext()
        self.callbacks = HostCallbacks()
        self.events = []
        for name in ("ssdp", "local_connect", "local_message", "printer_connected"):
            self.callbacks.set(name, lambda *args, name=name: self.events.append((name, *args)))
        poller = Poller(clock=self.clock, submit=self.jobs, jitter=lambda: 1.0)
        self.fleet = Fleet(
            self.callbacks,
            self.context,
            lambda code: f"<{code}>",
            poller=poller,
            clock=self.clock,
            client_factory=lambda snap: Client(self.server, snap.connection.base_url) if snap.connection else None,
            relay=self.relay,
        )

    def step(self, seconds=0.0):
        self.clock.advance(seconds)
        self.fleet.tick()
        self.jobs.run_all()

    def take(self, name=None):
        events, self.events = self.events, []
        return [e for e in events if name is None or e[0] == name]

    def requests(self):
        calls, self.server.calls = self.server.calls, []
        return calls

    def device_id(self, identity=IDENTITY, base=BASE):
        caps = self.server.capabilities()
        self.server.calls.pop()
        return dev_id(Binding.of(base, caps), identity)

    def connected(self):
        """Bound, discovered and connected to printer 1 with its first status accepted."""
        self.fleet.rebind(snapshot(1))
        self.fleet.discover()
        self.step()
        self.fleet.connect(self.device_id(), "12345678")
        self.step()
        self.take()
        self.requests()
        return self.device_id()


@pytest.fixture
def rig():
    return Rig()


def reports(events):
    return [json.loads(e[2])["print"] for e in events if e[0] == "local_message" and "print" in json.loads(e[2])]


# -- demand --------------------------------------------------------------------------------------------------------


def test_without_demand_the_farm_hears_nothing(rig):
    rig.fleet.rebind(snapshot(1))
    for _ in range(10):
        rig.step(30)
    assert rig.requests() == []
    assert rig.take() == []


def test_discovery_reads_the_list_once_and_announces_only_known_models(rig):
    rig.fleet.rebind(snapshot(1))
    rig.fleet.discover()
    rig.step()
    assert rig.requests() == ["capabilities", "printers"]
    (announced,) = rig.take("ssdp")
    payload = json.loads(announced[1])
    assert payload["dev_id"] == rig.device_id() and payload["dev_type"] == "BL-P001"
    for _ in range(5):
        rig.step(61)
    assert rig.requests() == []  # no tab, no connected device: one read was all


def test_the_tab_asks_for_the_list_every_minute_while_it_wants_the_fleet(rig):
    rig.fleet.rebind(snapshot(1))
    rig.fleet.want_fleet(True)
    rig.step()
    rig.step(59)
    assert rig.requests() == ["capabilities", "printers"]
    rig.step(1.5)
    assert rig.requests() == ["capabilities", "printers"]
    rig.fleet.want_fleet(False)
    rig.step(61)
    assert rig.requests() == []


def test_a_blocked_snapshot_reads_nothing_even_when_asked(rig):
    rig.fleet.rebind(snapshot(1, blocked="held"))
    rig.fleet.discover()
    rig.fleet.want_fleet(True)
    rig.step(61)
    assert rig.requests() == []


# -- connecting ----------------------------------------------------------------------------------------------------


def test_a_device_connects_once_its_first_status_is_accepted(rig):
    rig.fleet.rebind(snapshot(1))
    rig.fleet.discover()
    rig.step()
    rig.take()
    rig.requests()
    device = rig.device_id()
    assert rig.fleet.connect(device, "12345678") == 0
    assert rig.take() == []  # the UI thread is answered at once; the result follows the read
    rig.step()
    assert rig.requests() == ["capabilities", ("status", (1,))]
    events = rig.take()
    assert [e[0] for e in events] == ["local_message", "local_message", "local_connect", "printer_connected"]
    version = json.loads(events[0][2])
    assert version["info"]["module"] == [{"name": "ota", "sw_ver": "01.08.02.00"}]
    report = json.loads(events[1][2])["print"]
    assert report["msg"] == 0 and report["gcode_state"] == "IDLE"
    assert events[2] == ("local_connect", 0, device, "0")
    assert events[3] == ("printer_connected", device)


def test_connecting_before_the_list_was_read_reads_it_first(rig):
    rig.fleet.rebind(snapshot(1))
    device = rig.device_id()
    rig.fleet.connect(device, "x")
    rig.step()
    assert rig.requests() == ["capabilities", "printers"]
    rig.step()  # the next pass of the fleet's loop starts the device's first status read
    assert rig.requests() == ["capabilities", ("status", (1,))]
    assert ("local_connect", 0, device, "0") in rig.take()


@pytest.mark.parametrize("known_list", [True, False])
def test_a_device_of_another_binding_is_refused_with_a_sentence(rig, known_list):
    rig.fleet.rebind(snapshot(1))
    if known_list:
        rig.fleet.discover()
        rig.step()
        rig.take()
    old = dev_id(Binding(BASE, "server-1", "an-earlier-epoch", "actor-1"), IDENTITY)
    rig.fleet.connect(old, "x")
    rig.step()
    (refusal,) = rig.take("local_connect")
    assert refusal == ("local_connect", 1, old, "<connect.foreign>")
    assert refusal[3] not in ("5", "-1")
    rig.requests()
    rig.step(10)
    assert rig.requests() == []  # nothing is polled for a refused device


def test_a_printer_without_a_model_cannot_be_connected(rig):
    rig.fleet.rebind(snapshot(1))
    rig.fleet.discover()
    rig.step()
    unknown = rig.device_id(OTHER)
    rig.fleet.connect(unknown, "x")
    assert rig.take("local_connect") == [("local_connect", 1, unknown, "<connect.foreign>")]


@pytest.mark.parametrize(
    ("blocked", "message"),
    [
        ("missing", "connect.setup"),
        ("held", "connect.restart"),
        ("needs_restart", "connect.restart"),
        ("problem", "connect.setup"),
    ],
)
def test_without_a_connection_a_connect_is_refused_with_what_to_do(rig, blocked, message):
    rig.fleet.rebind(snapshot(1, blocked=blocked))
    rig.fleet.connect("bamdude-whatever", "x")
    assert rig.take("local_connect") == [("local_connect", 1, "bamdude-whatever", f"<{message}>")]


def test_a_connect_waiting_when_the_settings_lose_their_connection_hears_what_to_do(rig):
    rig.fleet.rebind(snapshot(1))
    device = rig.device_id()
    rig.fleet.connect(device, "x")  # waiting for the list
    rig.fleet.rebind(snapshot(2, blocked="needs_restart"))
    assert rig.take("local_connect") == [("local_connect", 1, device, "<connect.restart>")]
    rig.step(10)
    assert rig.requests() == []


def test_a_connected_device_goes_quiet_without_a_connection_and_comes_back_with_one(rig):
    device = rig.connected()
    rig.fleet.rebind(snapshot(2, blocked="held"))
    assert rig.take("local_connect") == []  # no refusal for a device that was connected
    rig.step(10)
    assert rig.requests() == [] and reports(rig.take()) == []
    rig.fleet.rebind(snapshot(3))
    rig.step()
    assert rig.fleet.target.dev_id == device and rig.fleet.fresh


def test_a_connect_whose_read_is_refused_by_the_server_hears_it(rig):
    rig.fleet.rebind(snapshot(1))
    device = rig.device_id()
    rig.server.fail["capabilities"] = HttpError(401, "unauthorized", "")
    rig.fleet.connect(device, "x")
    rig.step()
    rig.step()
    assert rig.take("local_connect") == [("local_connect", 1, device, "<connect.refused>")]


# -- freshness and pushing -----------------------------------------------------------------------------------------


def test_a_fresh_device_gets_a_full_report_every_three_seconds(rig):
    rig.connected()
    rig.step(PUSH_SECONDS - 0.1)
    assert reports(rig.take()) == []
    rig.step(0.1)
    (report,) = reports(rig.take())
    assert report["command"] == "push_status" and report["msg"] == 0


def test_without_an_accepted_status_for_thirty_seconds_nothing_is_pushed(rig):
    rig.connected()
    rig.server.fail["status"] = TransportError("timeout")
    for _ in range(int(STALE_SECONDS)):
        rig.server.fail.setdefault("status", TransportError("timeout"))
        rig.step(1)
    assert not rig.fleet.fresh
    rig.take()
    rig.step(PUSH_SECONDS)
    assert reports(rig.take()) == []
    assert all(e[0] != "local_connect" for e in rig.events)  # no "lost" is ever sent


def test_a_stale_selected_device_keeps_one_status_read_and_recovers_without_a_gesture(rig):
    rig.connected()
    for _ in range(40):
        rig.server.fail["status"] = TransportError("connect")
        rig.step(1)
    assert not rig.fleet.fresh
    rig.requests()
    rig.fleet.want_fleet(False)  # anything that weighs the demand again, while stale
    for _ in range(70):
        rig.server.fail["status"] = TransportError("connect")
        rig.step(1)
    calls = rig.requests()
    assert ("status", (1,)) in calls  # stale is not "stop asking"
    assert "printers" not in calls  # and the fleet itself is not read
    rig.take()
    rig.server.fail.clear()  # the farm answers again
    for _ in range(70):  # the backoff tops out at a minute; the next read succeeds
        rig.step(1)
        if rig.fleet.fresh:
            break
    assert rig.fleet.fresh
    rig.step(PUSH_SECONDS)
    assert reports(rig.take())
    assert all(e[0] != "local_connect" for e in rig.events)


def test_one_status_read_is_in_flight_at_a_time(rig):
    rig.connected()
    rig.clock.advance(10)
    rig.fleet.tick()
    rig.fleet.tick()
    rig.clock.advance(10)
    rig.fleet.tick()
    assert len(rig.jobs.queued) == 1


def test_an_authentication_failure_stops_the_selected_devices_reads_until_the_settings_change(rig):
    rig.connected()
    rig.server.fail["capabilities"] = HttpError(401, "unauthorized", "")
    rig.step(5)
    rig.requests()
    for _ in range(5):
        rig.step(30)
    assert rig.requests() == []
    assert not rig.fleet.fresh


# -- the binding ---------------------------------------------------------------------------------------------------


def test_a_restore_ends_the_device_and_announces_the_new_binding(rig):
    old = rig.connected()
    rig.server.epoch = "epoch-2"
    rig.step(5)
    assert rig.fleet.target is None  # the status read itself ends the device
    rig.step()  # and the list is read again at the loop's next pass
    announced = [json.loads(e[1])["dev_id"] for e in rig.take("ssdp")]
    new = rig.device_id()
    assert announced == [new] and new != old
    rig.step(PUSH_SECONDS)
    assert reports(rig.take()) == []  # the old device hears nothing more
    rig.fleet.connect(old, "x")
    assert rig.take("local_connect") == [("local_connect", 1, old, "<connect.foreign>")]


def test_another_key_on_the_same_server_ends_the_device(rig):
    rig.connected()
    rig.server.actor = "actor-2"
    rig.step(5)
    assert rig.fleet.target is None


def test_a_status_about_another_printer_with_the_same_number_is_not_this_devices(rig):
    rig.connected()
    rig.server.printers[0] = {"id": 1, "name": "New", "connector_identity": "a-new-printer", "model_code": "C12"}
    rig.server.statuses[1] = Server.status(connector_identity="a-new-printer", state="RUNNING")
    rig.take()
    rig.step(5)
    assert rig.fleet.target is not None  # not accepted, not dropped yet: the list decides
    rig.step()
    assert rig.requests() == ["capabilities", ("status", (1,)), "capabilities", "printers"]
    assert rig.fleet.target is None  # the list confirms it: the printer behind this device is gone
    rig.step(PUSH_SECONDS)
    assert all(r.get("gcode_state") != "RUNNING" for r in reports(rig.take()))


def test_a_missing_status_rereads_the_list_once_not_every_poll(rig):
    rig.connected()
    del rig.server.statuses[1]  # the list still has it; only its status is missing
    for _ in range(4):
        rig.step(5)
    assert rig.requests().count("printers") == 1
    assert rig.fleet.target is not None


def test_a_deleted_printer_drops_the_device(rig):
    rig.connected()
    rig.server.printers = rig.server.printers[1:]
    del rig.server.statuses[1]
    rig.step(5)
    rig.step()
    assert rig.fleet.target is None


def test_a_new_generation_with_the_same_binding_keeps_the_device(rig):
    device = rig.connected()
    rig.fleet.rebind(snapshot(2))  # e.g. another CA: a new generation, the same binding
    assert not rig.fleet.fresh  # nothing read under the old generation counts
    rig.step()
    assert rig.fleet.target is not None and rig.fleet.target.dev_id == device
    assert rig.fleet.fresh
    assert rig.relay.rotations == 2  # the relay's secret goes with every generation


def test_a_new_address_ends_the_device_at_its_next_read(rig):
    rig.connected()
    rig.fleet.rebind(snapshot(2, base="https://lan.example"))
    rig.step()
    assert rig.fleet.target is None
    assert not rig.fleet.fresh


def test_a_late_answer_of_an_older_generation_changes_nothing(rig):
    rig.fleet.rebind(snapshot(1))
    rig.fleet.discover()
    rig.fleet.tick()  # the read is out, not answered yet
    rig.fleet.rebind(snapshot(2, base="https://lan.example"))
    rig.jobs.run_all()
    assert rig.take("ssdp") == []
    assert rig.fleet.devices is None


class SplitPoller(Poller):
    """A poller whose reads the test runs by hand: fetch now, deliver later -- the window between the poller's own
    generation check and the delivery, which only the fleet's check closes."""

    def __init__(self, clock):
        super().__init__(clock=clock, submit=lambda job: None)
        self.reads = {}

    def add(self, name, interval, fetch, deliver):
        super().add(name, interval, fetch, deliver)
        self.reads[name] = (fetch, deliver)


@pytest.mark.parametrize("channel", ["printers", "status"])
def test_an_answer_delivered_after_a_rebind_changes_nothing(channel):
    rig = Rig()
    split = SplitPoller(rig.clock)
    rig.fleet = Fleet(
        rig.callbacks,
        rig.context,
        lambda code: f"<{code}>",
        poller=split,
        clock=rig.clock,
        client_factory=lambda snap: Client(rig.server, snap.connection.base_url) if snap.connection else None,
        relay=rig.relay,
    )
    rig.fleet.rebind(snapshot(1))
    fetch, deliver = split.reads["printers"]
    deliver(fetch())
    device = rig.device_id()
    rig.fleet.connect(device, "x")
    rig.take()
    fetch, deliver = split.reads[channel]
    answer = fetch()  # read under generation 1
    rig.fleet.rebind(snapshot(2))  # the same binding, a new generation
    deliver(answer)
    assert rig.take() == []  # no SSDP, no connect sequence from the old answer
    assert not rig.fleet.fresh
    assert rig.fleet.devices is None


def test_an_older_or_repeated_snapshot_is_ignored(rig):
    rig.fleet.rebind(snapshot(3))
    rig.fleet.rebind(snapshot(2, base="https://lan.example"))
    rig.fleet.rebind(snapshot(3, base="https://lan.example"))
    rig.fleet.discover()
    rig.step()
    assert json.loads(rig.take("ssdp")[0][1])["dev_ip"] == "farm.example"


def test_the_servers_polling_floor_lengthens_the_status_interval(rig):
    rig.connected()
    rig.server.capabilities = lambda: Capabilities(
        "server-1", "epoch-1", "actor-1", "0.7.3", True, {}, {"poll_min_seconds": 20}
    )
    rig.step(5)  # this answer brings the floor; its own next read was already planned 5 s on
    rig.step(5)
    rig.requests()
    rig.step(19.9)
    assert rig.requests() == []
    rig.step(0.2)
    assert ("status", (1,)) in rig.requests()


# -- commands ------------------------------------------------------------------------------------------------------


def test_report_requests_are_answered_from_the_cache_while_fresh(rig):
    device = rig.connected()
    assert rig.fleet.command(device, json.dumps({"pushing": {"command": "pushall"}})) == 0
    assert len(reports(rig.take())) == 1
    assert rig.fleet.command(device, json.dumps({"info": {"command": "get_version"}})) == 0
    events = rig.take()
    assert json.loads(events[0][2])["info"]["module"][0]["sw_ver"] == "01.08.02.00"
    assert rig.requests() == []  # from the cache: no extra read


def test_report_requests_are_not_answered_while_stale(rig):
    device = rig.connected()
    rig.clock.advance(STALE_SECONDS + 1)
    assert rig.fleet.command(device, json.dumps({"print": {"command": "push_status"}})) == 0
    assert rig.fleet.command(device, json.dumps({"info": {"command": "get_version"}})) == 0
    assert rig.take() == []


def test_the_access_code_is_echoed(rig):
    device = rig.connected()
    assert rig.fleet.command(device, json.dumps({"system": {"command": "get_access_code"}})) == 0
    (event,) = rig.take()
    assert json.loads(event[2]) == {"system": {"command": "get_access_code", "access_code": "12345678"}}


@pytest.mark.parametrize(
    "body", [{"print": {"command": "pause"}}, {"print": {"command": "gcode_line", "param": "G28"}}, {}, "nope", None]
)
def test_any_other_command_is_not_supported(rig, body):
    device = rig.connected()
    assert rig.fleet.command(device, json.dumps(body) if body is not None else "{") == -7010
    assert rig.take() == []


def test_a_command_for_another_device_is_not_answered(rig):
    rig.connected()
    assert rig.fleet.command("bamdude-someone-else", json.dumps({"pushing": {"command": "pushall"}})) == 0
    assert rig.take() == []


# -- jobs and the camera -------------------------------------------------------------------------------------------


def test_a_job_is_admitted_only_for_the_connected_fresh_device(rig):
    device = rig.connected()
    assert rig.fleet.admit_job(device).printer_id == 1
    assert rig.fleet.admit_job("bamdude-someone-else") == "foreign"
    rig.clock.advance(STALE_SECONDS)
    assert rig.fleet.admit_job(device) == "stale"


def test_a_job_before_any_connection_is_foreign(rig):
    rig.fleet.rebind(snapshot(1))
    assert rig.fleet.admit_job("bamdude-anything") == "foreign"


def test_the_camera_url_is_the_relays_for_the_connected_fresh_device(rig):
    rig.connected()
    url = rig.fleet.camera_url()
    assert url.endswith(f"/{IDENTITY}") and url.startswith("http://127.0.0.1:")
    assert rig.relay.target == Target(1, IDENTITY)
    rig.clock.advance(STALE_SECONDS)
    assert rig.fleet.camera_url() == ""


def test_no_camera_without_the_keys_right(rig):
    rig.server.features = {}
    rig.connected()
    assert rig.fleet.camera_url() == ""


def test_the_relay_opens_the_stream_through_the_current_generations_client(rig):
    rig.connected()
    rig.fleet.rebind(snapshot(2, base="https://farm.example/bamdude"))
    assert rig.fleet.open_camera(Target(1, IDENTITY)) == "stream"
    assert rig.server.calls[-1] == ("camera", 1, IDENTITY, "https://farm.example/bamdude")


def test_disconnect_stops_the_reads_and_the_camera(rig):
    rig.connected()
    rig.fleet.camera_url()
    rig.fleet.disconnect()
    assert rig.relay.target is None
    rig.step(30)
    assert rig.requests() == []


# -- the fence -----------------------------------------------------------------------------------------------------


def test_nothing_reaches_the_host_after_the_fence(rig):
    device = rig.connected()
    rig.context.close()
    rig.step(PUSH_SECONDS)
    rig.fleet.command(device, json.dumps({"pushing": {"command": "pushall"}}))
    rig.fleet.connect("bamdude-x", "x")
    assert rig.take() == []


def test_a_callback_the_host_cleared_is_not_called(rig):
    device = rig.connected()
    rig.callbacks.set("local_message", None)
    rig.step(PUSH_SECONDS)
    rig.fleet.command(device, json.dumps({"pushing": {"command": "pushall"}}))
    assert rig.take("local_message") == []


def test_stop_ends_the_loop_and_the_relay(rig):
    rig.fleet.rebind(snapshot(1))
    rig.fleet.start()
    assert rig.relay.started
    assert rig.fleet.stop(timeout=2) is True
    assert rig.relay.stopped
