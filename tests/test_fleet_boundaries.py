"""The fleet's boundaries the H2 review found open (R1-R4): nothing of an older context reaches the host or the farm.

Codex's reproductions (research-artifacts/orca-bamdude-h2-review-2026-11-06/test_h2_edges.py) are kept here in the
suite's own form, with the positive controls the review asked for. Network traffic is loopback only.
"""

import http.client
import json
import threading
import time
from urllib.parse import urlsplit

import pytest
from fakes import Server, json_answer
from test_fleet import IDENTITY, OTHER, STALE_SECONDS, Client, Rig, snapshot

from orca_bamdude.api import ConnectorClient
from orca_bamdude.camera_relay import CameraRelay, Target
from orca_bamdude.config import Connection
from orca_bamdude.fleet import Fleet
from orca_bamdude.service import Runtime
from orca_bamdude.transport import Transport

NEW = "https://other.example/new"


def _paused_emits(rig, monkeypatch):
    """Hold every host message just before it is handed over; the test releases them."""
    entered, release = threading.Event(), threading.Event()
    emit = rig.fleet._emit

    def paused(name, *args):
        entered.set()
        assert release.wait(4)
        return emit(name, *args)

    monkeypatch.setattr(rig.fleet, "_emit", paused)
    return entered, release


# -- R2: a message computed under an older context is dropped at the moment it would be handed over -------------


@pytest.mark.parametrize("kind", ["connect", "push", "ssdp"])
def test_a_message_of_an_older_generation_never_reaches_the_host(monkeypatch, kind):
    rig = Rig()
    if kind == "connect":
        rig.fleet.rebind(snapshot(1))
        rig.fleet.discover()
        rig.step()
        rig.fleet.connect(rig.device_id())
        answer = rig.fleet._fetch_status()
        work = lambda: rig.fleet._deliver_status(answer)  # noqa: E731
    elif kind == "ssdp":
        rig.fleet.rebind(snapshot(1))
        answer = rig.fleet._fetch_printers()
        work = lambda: rig.fleet._deliver_printers(answer)  # noqa: E731
    else:
        rig.connected()
        work = lambda: rig.fleet._push(force=True)  # noqa: E731
    entered, release = _paused_emits(rig, monkeypatch)
    rig.take()
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(2)
        rig.fleet.rebind(snapshot(2, base=NEW))
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert rig.take() == []
    finally:
        release.set()
        worker.join(3)


def test_a_connect_sequence_dropped_by_a_rebind_is_sent_again_under_the_same_binding(monkeypatch):
    # Positive control: a CA or proxy-header change keeps the binding; the device must still end up connected.
    rig = Rig()
    rig.fleet.rebind(snapshot(1))
    rig.fleet.discover()
    rig.step()
    device = rig.device_id()
    rig.fleet.connect(device)
    answer = rig.fleet._fetch_status()
    entered, release = _paused_emits(rig, monkeypatch)
    rig.take()
    worker = threading.Thread(target=lambda: rig.fleet._deliver_status(answer))
    worker.start()
    assert entered.wait(2)
    rig.fleet.rebind(snapshot(2))  # the same address and namespace: the same binding
    release.set()
    worker.join(3)
    monkeypatch.undo()
    assert rig.take() == []
    rig.step()
    assert ("local_connect", 0, device, "0") in rig.take()
    assert rig.fleet.fresh


def test_an_older_list_cannot_bring_back_a_namespace_already_rejected():
    rig = Rig()
    old = rig.connected()
    old_list = rig.fleet._fetch_printers()
    rig.server.epoch = "epoch-after-restore"
    rig.fleet._deliver_status(rig.fleet._fetch_status())  # the status read sees the restore and ends the device
    assert rig.fleet.target is None
    rig.take()
    rig.fleet._deliver_printers(old_list)  # the list read before the restore arrives last
    assert rig.fleet.devices is None or rig.fleet.devices.get(old) is None
    assert rig.take("ssdp") == []


def test_after_a_rejected_namespace_the_new_one_is_read_and_announced():
    rig = Rig()
    old = rig.connected()
    rig.server.epoch = "epoch-after-restore"
    rig.step(5)
    rig.step()
    announced = [json.loads(e[1])["dev_id"] for e in rig.take("ssdp")]
    assert announced and old not in announced


# -- R4: a status naming another printer revokes at once --------------------------------------------------------


@pytest.mark.parametrize("list_reread", ["pending", "failing"])
def test_a_status_naming_another_printer_revokes_job_push_and_camera_at_once(list_reread):
    from orca_bamdude.transport import TransportError

    rig = Rig()
    device = rig.connected()
    rig.fleet.camera_url()
    rig.server.statuses[1] = rig.server.status(connector_identity=OTHER)
    if list_reread == "failing":
        rig.server.fail["printers"] = TransportError("connect")
    rig.step(5)
    assert rig.fleet.admit_job(device) == "stale"
    assert rig.fleet.camera_url() == ""
    assert rig.relay.target is None
    rig.take()
    rig.step(3)
    assert [e for e in rig.take() if e[0] == "local_message"] == []  # no report of the wrong printer


def test_a_missing_status_is_not_a_proof_and_only_ages():
    rig = Rig()
    device = rig.connected()
    del rig.server.statuses[1]
    rig.step(5)
    assert rig.fleet.admit_job(device).dev_id == device  # still within the last accepted status's 30 s
    rig.clock.advance(STALE_SECONDS)
    assert rig.fleet.admit_job(device) == "stale"


# -- R1: the camera follows its admission, at the address, the open and every renewal ---------------------------


class Picture:
    content_type = "multipart/x-mixed-replace; boundary=frame"

    def __init__(self, end_after_frame=False):
        self.first = True
        self.end_after_frame = end_after_frame
        self.closed = threading.Event()

    def read(self, size):
        if self.first:
            self.first = False
            return b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8picture\xff\xd9\r\n"
        if not self.end_after_frame:
            self.closed.wait(4)
        return b""

    def close(self):
        self.closed.set()


def _real_relay(rig, admits=True):
    relay = CameraRelay(rig.fleet.open_camera, admits=rig.fleet.camera_admits if admits else None)
    rig.relay = rig.fleet.relay = relay
    return relay


def _get(url):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=3)
    conn.request("GET", parts.path)
    response = conn.getresponse()
    return conn, response


@pytest.mark.parametrize("admits", [True, False])
def test_a_kept_camera_address_opens_nothing_once_the_device_is_stale(monkeypatch, admits):
    # admits=False: the relay as the review built it, without the owner's admission; the fleet alone must hold.
    rig = Rig()
    opened = []
    relay = _real_relay(rig, admits)
    rig.connected()
    monkeypatch.setattr(Client, "open_camera", lambda client, pid, identity: opened.append(1) or Picture())
    relay.start()
    try:
        url = rig.fleet.camera_url()
        rig.clock.advance(STALE_SECONDS)
        assert rig.fleet.camera_url() == ""
        conn, response = _get(url)
        status = response.status
        response.close()
        conn.close()
        assert status == 404 and opened == []
    finally:
        relay.stop(2)


@pytest.mark.parametrize("admits", [True, False])
def test_a_camera_address_formed_before_a_rebind_never_targets_the_new_client(monkeypatch, admits):
    rig = Rig()
    relay = _real_relay(rig, admits)
    rig.connected()
    entered, release = threading.Event(), threading.Event()
    set_target = relay.set_target

    def paused(target):
        if target is not None:
            entered.set()
            assert release.wait(4)
        set_target(target)

    monkeypatch.setattr(relay, "set_target", paused)
    urls = []
    worker = threading.Thread(target=lambda: urls.append(rig.fleet.camera_url()))
    worker.start()
    try:
        assert entered.wait(2)
        rig.fleet.rebind(snapshot(2, base=NEW))
        release.set()
        worker.join(3)
        assert urls == [""]
        assert relay.target is None
        with pytest.raises(Exception):  # noqa: B017 -- whatever the refusal, the new client is not asked
            rig.fleet.open_camera(Target(1, IDENTITY))
        assert not [c for c in rig.requests() if isinstance(c, tuple) and c[0] == "camera"]
    finally:
        release.set()
        worker.join(3)


def test_an_old_camera_intent_cannot_read_a_new_server_over_http(monkeypatch):
    stream_end = threading.Event()

    def chunks():
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8new\xff\xd9\r\n"
        stream_end.wait(4)

    server = Server(lambda m, p: (200, {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}, chunks()))

    def http_camera(client, pid, identity):
        connection = Connection.build(client.base_url, "bd_test_synthetic")
        return ConnectorClient(Transport(connection, timeout=2)).open_camera(pid, identity)

    monkeypatch.setattr(Client, "open_camera", http_camera)
    rig = Rig()
    relay = _real_relay(rig, admits=False)
    rig.connected()
    relay.start()
    entered, release = threading.Event(), threading.Event()
    set_target = relay.set_target

    def paused(target):
        if target is not None:
            entered.set()
            assert release.wait(4)
        set_target(target)

    monkeypatch.setattr(relay, "set_target", paused)
    urls = []
    worker = threading.Thread(target=lambda: urls.append(rig.fleet.camera_url()))
    worker.start()
    try:
        assert entered.wait(2)
        rig.fleet.rebind(snapshot(2, base=server.url))
        release.set()
        worker.join(3)
        assert urls == [""] and server.seen == []
    finally:
        release.set()
        stream_end.set()
        worker.join(3)
        relay.stop(2)
        server.close()


@pytest.mark.parametrize("loss", ["stale", "disconnect", "rebind", "right_lost", "fence"])
def test_a_playing_camera_ends_when_its_admission_does(monkeypatch, loss):
    rig = Rig()
    pictures = []
    relay = _real_relay(rig)
    rig.connected()

    def open_camera(client, pid, identity):
        picture = Picture()
        pictures.append(picture)
        return picture

    monkeypatch.setattr(Client, "open_camera", open_camera)
    relay.start()
    conn = None
    try:
        url = rig.fleet.camera_url()
        conn, response = _get(url)
        assert response.status == 200 and response.read(8)
        if loss == "stale":
            rig.clock.advance(STALE_SECONDS)
        elif loss == "disconnect":
            rig.fleet.disconnect()
        elif loss == "rebind":
            rig.fleet.rebind(snapshot(2, base=NEW))
        elif loss == "right_lost":
            rig.server.features = {}
            rig.step(5)
        else:
            rig.context.close()
        rig.fleet.tick()
        assert pictures[0].closed.wait(3)  # the viewer was let go and its upstream closed
        assert relay.target is None or loss == "fence"
    finally:
        if conn is not None:
            conn.close()
        relay.stop(2)


def test_a_renewal_after_the_lease_asks_again_and_an_admitted_one_still_plays(monkeypatch):
    rig = Rig()
    pictures = []
    relay = _real_relay(rig)
    rig.connected()

    def open_camera(client, pid, identity):
        picture = Picture(end_after_frame=not pictures)  # the first lease ends after its picture
        pictures.append(picture)
        return picture

    monkeypatch.setattr(Client, "open_camera", open_camera)
    relay.start()
    try:
        conn, response = _get(rig.fleet.camera_url())
        assert response.status == 200
        deadline = time.monotonic() + 3
        while len(pictures) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(pictures) == 2  # renewed under the same viewer while admitted
        conn.close()
    finally:
        relay.stop(2)


# -- R3: an unload cancels what is in flight and starts nothing after it ----------------------------------------


def test_an_unload_cancels_a_read_and_no_request_follows_it(tmp_path):
    entered, release, followup = threading.Event(), threading.Event(), threading.Event()
    caps = {
        "product": "bamdude",
        "protocol_version": 1,
        "connector_server_id": "server-1",
        "storage_epoch": "epoch-1",
        "actor_id": "actor-1",
        "features": {},
        "limits": {},
    }

    def routes(method, path):
        if path.endswith("/capabilities"):
            entered.set()
            release.wait(8)
            return json_answer(200, caps)
        if path.endswith("/printers/"):
            followup.set()
            return json_answer(200, [])
        return json_answer(404, {})

    server = Server(routes)
    runtime = Runtime(lambda *a: None, str(tmp_path), "L", requested={"network:http://127.0.0.1"}, decision={})
    try:
        runtime.apply_settings({"server_url": server.url, "api_key": "bd_test_synthetic"}, None, None)
        assert runtime.flush(2)
        runtime.start_fleet()
        runtime.fleet.discover()
        assert entered.wait(3)
        report = runtime.shutdown("unload")
        with runtime.fleet.poller._lock:
            alive = [t for t in runtime.fleet.poller._workers if t.is_alive()]
        release.set()
        assert alive == [] and report["steps"]["stop"] == "ok"
        assert not followup.wait(1.5)
    finally:
        release.set()
        server.close()


def test_an_unload_ends_a_camera_waiting_for_its_headers(tmp_path, monkeypatch):
    release = threading.Event()

    def routes(method, path):
        if "camera/stream" in path:
            release.wait(8)
        return json_answer(404, {})

    server = Server(routes)
    rig = Rig()
    fleet = Fleet(
        rig.callbacks,
        rig.context,
        lambda code: code,
        clock=rig.clock,
        client_factory=lambda snap: ConnectorClient(Transport(Connection.build(server.url, "bd_k"), timeout=8)),
    )
    fleet.rebind(snapshot(1, base=server.url))
    opened = {}

    def open_it():
        try:
            opened["stream"] = fleet._client.open_camera(1, IDENTITY)
        except Exception as exc:  # noqa: BLE001
            opened["error"] = type(exc).__name__

    worker = threading.Thread(target=open_it)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not server.seen and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        assert fleet.stop(2)
        worker.join(2)
        assert not worker.is_alive() and time.monotonic() - started < 2.5
        assert "error" in opened
    finally:
        release.set()
        worker.join(5)
        server.close()


def test_a_rebind_cancels_the_older_generations_reads():
    rig = Rig()
    rig.fleet.rebind(snapshot(1))
    first = rig.fleet._client
    rig.fleet.rebind(snapshot(2, base=NEW))
    assert first.closed and not rig.fleet._client.closed


def test_a_rebind_waits_for_a_hand_over_already_in_progress():
    # After rebind returns, nothing computed under the old generation is handed to the host: one already being handed
    # over finishes first (the host's callbacks only re-post, so the wait is short).
    rig = Rig()
    rig.connected()
    inside, release = threading.Event(), threading.Event()

    def slow_host(*args):
        inside.set()
        release.wait(4)

    rig.callbacks.set("local_message", slow_host)
    pusher = threading.Thread(target=lambda: rig.fleet._push(force=True))
    pusher.start()
    assert inside.wait(2)
    rebinder = threading.Thread(target=lambda: rig.fleet.rebind(snapshot(2, base=NEW)))
    rebinder.start()
    try:
        time.sleep(0.2)
        assert rebinder.is_alive()  # waits for the hand-over that had begun
    finally:
        release.set()
        pusher.join(3)
        rebinder.join(3)
    assert not rebinder.is_alive()


@pytest.mark.parametrize("read", ["printers", "status"])
def test_a_reads_next_request_is_not_sent_once_the_fence_is_up(read):
    # The chain itself stops at the fence, whatever the client does with a close (here: nothing).
    rig = Rig()
    rig.connected()
    caps = rig.server.capabilities

    def capabilities_then_unload():
        answer = caps()
        rig.context.close()  # the plugin is turned off while the first request of the chain was out
        return answer

    rig.server.capabilities = capabilities_then_unload
    rig.requests()
    with pytest.raises(RuntimeError):
        rig.fleet._fetch_printers() if read == "printers" else rig.fleet._fetch_status()
    assert rig.requests() == ["capabilities"]


def test_a_camera_admission_once_lost_comes_back_only_through_a_new_address():
    rig = Rig()
    rig.connected()
    url = rig.fleet.camera_url()
    target = rig.relay.target
    assert url and rig.fleet.camera_admits(target)
    rig.clock.advance(STALE_SECONDS)
    rig.fleet.tick()  # stale: the camera is taken down
    rig.step()  # fresh again
    assert rig.fleet.fresh
    assert not rig.fleet.camera_admits(target)  # the old admission is not regained by itself
    assert rig.fleet.camera_url() and rig.fleet.camera_admits(target)  # OrcaSlicer asks again: a new one


def test_the_runtimes_relay_asks_the_fleet(tmp_path):
    runtime = Runtime(lambda *a: None, str(tmp_path), "L", requested=set(), decision={})
    # Nothing is connected, so the fleet admits nothing -- and neither may the relay it built.
    assert runtime.relay._admitted(Target(1, IDENTITY)) is False


def test_an_announcement_of_a_replaced_map_is_dropped(monkeypatch):
    # Within one era a newer list may drop a printer; the older list's announcement of it must not be handed over.
    rig = Rig()
    rig.fleet.rebind(snapshot(1))
    rig.server.printers.append({"id": 3, "name": "Removed later", "connector_identity": "gone", "model_code": "C12"})
    first = rig.fleet._fetch_printers()
    rig.server.printers.pop()
    second = rig.fleet._fetch_printers()
    entered, release = threading.Event(), threading.Event()
    emit, held = rig.fleet._emit, []

    def pause_first(name, *args):
        if not held:
            held.append(1)
            entered.set()
            assert release.wait(4)
        return emit(name, *args)

    monkeypatch.setattr(rig.fleet, "_emit", pause_first)
    worker = threading.Thread(target=lambda: rig.fleet._deliver_printers(first))
    worker.start()
    try:
        assert entered.wait(2)
        rig.fleet._deliver_printers(second)
        rig.take()
        release.set()
        worker.join(3)
        late = [json.loads(e[1])["dev_id"] for e in rig.take("ssdp")]
        assert late == []  # nothing of the replaced map, the removed printer least of all
    finally:
        release.set()
        worker.join(3)


# -- the fix review: a proven namespace change revokes the camera's address; revocations meet the hand-over --------


def _camera_rig(monkeypatch):
    rig = Rig()
    opened = []
    monkeypatch.setattr(Client, "open_camera", lambda client, pid, identity: opened.append(1) or Picture())
    relay = _real_relay(rig)
    relay.start()
    return rig, relay, opened


def _status_of(url):
    conn, response = _get(url)
    status = response.status
    response.close()
    conn.close()
    return status


@pytest.mark.parametrize("seen_by", ["status", "list"])
def test_a_namespace_change_revokes_the_old_camera_address_for_good(monkeypatch, seen_by):
    rig, relay, opened = _camera_rig(monkeypatch)
    try:
        old = rig.connected()
        old_url = rig.fleet.camera_url()
        rig.server.epoch = "epoch-after-restore"  # the same printers, their numbers and identities kept
        if seen_by == "status":
            rig.step(5)
        else:
            rig.fleet.want_fleet(True)
            rig.fleet.poller.wake("printers")
            rig.fleet.tick()  # only the list is due: the status read is still 5 s away
            rig.jobs.run_all()
        rig.step()
        new = rig.device_id()
        assert new != old
        rig.fleet.connect(new)
        rig.step()
        new_url = rig.fleet.camera_url()
        assert new_url and new_url != old_url
        assert _status_of(old_url) == 404 and opened == []  # the earlier namespace's address opens nothing
    finally:
        relay.stop(2)


def test_an_ordinary_status_poll_keeps_the_camera_address(monkeypatch):
    rig, relay, opened = _camera_rig(monkeypatch)
    try:
        rig.connected()
        url = rig.fleet.camera_url()
        for _ in range(4):
            rig.step(5)
        assert rig.fleet.camera_url() == url
    finally:
        relay.stop(2)


def test_a_revocation_proven_after_the_stamp_check_still_stops_the_hand_over(monkeypatch):
    # The fix review's window: the stamp was checked, the host call has not begun (something holds the callbacks'
    # own lock, as a set_on_* does), and meanwhile a status proves the namespace changed.
    rig = Rig()
    rig.connected()
    rig.take()
    checked = threading.Event()
    original = rig.fleet._holds_locked

    def holds(stamp):
        result = original(stamp)
        if result:
            checked.set()
        return result

    monkeypatch.setattr(rig.fleet, "_holds_locked", holds)
    rig.callbacks._lock.acquire()
    worker = threading.Thread(target=lambda: rig.fleet._push(force=True))
    worker.start()
    try:
        assert checked.wait(2)
        rig.server.epoch = "epoch-after-restore"
        rig.fleet._deliver_status(rig.fleet._fetch_status())
        assert rig.fleet.target is None
    finally:
        rig.callbacks._lock.release()
        worker.join(3)
    assert rig.take() == []


def test_a_revocation_waits_for_a_hand_over_in_progress():
    rig = Rig()
    rig.connected()
    inside, release = threading.Event(), threading.Event()
    rig.callbacks.set("local_message", lambda *args: (inside.set(), release.wait(4)))
    pusher = threading.Thread(target=lambda: rig.fleet._push(force=True))
    pusher.start()
    assert inside.wait(2)
    rig.server.epoch = "epoch-after-restore"
    answer = rig.fleet._fetch_status()
    revoker = threading.Thread(target=lambda: rig.fleet._deliver_status(answer))
    revoker.start()
    try:
        time.sleep(0.2)
        assert revoker.is_alive() and rig.fleet.target is not None  # the hand-over that began finishes first
    finally:
        release.set()
        pusher.join(3)
        revoker.join(3)
    assert rig.fleet.target is None


@pytest.mark.parametrize("change", ["disconnect", "connect_other", "list_without_it"])
def test_other_revocations_meet_the_hand_over_too(monkeypatch, change):
    rig = Rig()
    rig.connected()
    rig.take()
    checked = threading.Event()
    original = rig.fleet._holds_locked
    monkeypatch.setattr(rig.fleet, "_holds_locked", lambda stamp: original(stamp) and (checked.set() or True))
    rig.callbacks._lock.acquire()
    worker = threading.Thread(target=lambda: rig.fleet._push(force=True))
    worker.start()
    try:
        assert checked.wait(2)
        if change == "disconnect":
            rig.fleet.disconnect()
        elif change == "connect_other":
            rig.fleet.connect("bamdude-another")
        else:
            rig.server.printers = rig.server.printers[1:]
            rig.fleet._deliver_printers(rig.fleet._fetch_printers())
    finally:
        rig.callbacks._lock.release()
        worker.join(3)
    assert [e for e in rig.take() if e[0] == "local_message"] == []


def test_a_rebind_from_inside_a_callback_neither_hangs_nor_loses_the_connect():
    rig = Rig()
    rig.fleet.rebind(snapshot(1))
    rig.fleet.discover()
    rig.step()
    device = rig.device_id()
    rig.fleet.connect(device)
    native = rig.callbacks._fns["local_message"]
    rebound = []

    def callback(*args):
        native(*args)
        if not rebound:
            rebound.append(1)
            rig.fleet.rebind(snapshot(2))

    rig.callbacks.set("local_message", callback)
    worker = threading.Thread(target=rig.step)
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    rig.take()
    rig.step()
    assert ("local_connect", 0, device, "0") in rig.take()
    assert rig.fleet.fresh
