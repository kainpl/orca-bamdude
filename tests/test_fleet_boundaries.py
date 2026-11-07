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


def _issue_across(rig, relay, monkeypatch, change):
    """Pause ``camera_url`` between its decision and the relay (the H2 review's window), run ``change`` beside it, and
    return what was issued, whether the change waited for it, and the change's thread. Since fix2 R2 the change waits:
    the address is issued under the old namespace, and the change then revokes it."""
    entered, release = threading.Event(), threading.Event()
    set_target = relay.set_target

    def paused(target):
        if target is not None and not entered.is_set():
            entered.set()
            assert release.wait(4)
        set_target(target)

    monkeypatch.setattr(relay, "set_target", paused)
    urls = []
    worker = threading.Thread(target=lambda: urls.append(rig.fleet.camera_url()))
    worker.start()
    changer = threading.Thread(target=change)
    try:
        assert entered.wait(2)
        changer.start()
        time.sleep(0.2)
        waited = changer.is_alive()
    finally:
        release.set()
        worker.join(3)
        if changer.ident is not None:
            changer.join(3)
    assert not changer.is_alive()
    return urls, waited


@pytest.mark.parametrize("admits", [True, False])
def test_a_camera_address_formed_before_a_rebind_never_targets_the_new_client(monkeypatch, admits):
    rig = Rig()
    relay = _real_relay(rig, admits)
    rig.connected()
    urls, waited = _issue_across(rig, relay, monkeypatch, lambda: rig.fleet.rebind(snapshot(2, base=NEW)))
    assert waited and len(urls) == 1  # the rebind waited for the address being issued
    assert relay.target is None
    with pytest.raises(Exception):  # noqa: B017 -- whatever the refusal, the new client is not asked
        rig.fleet.open_camera(Target(1, IDENTITY))
    assert not [c for c in rig.requests() if isinstance(c, tuple) and c[0] == "camera"]


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
    try:
        urls, waited = _issue_across(rig, relay, monkeypatch, lambda: rig.fleet.rebind(snapshot(2, base=server.url)))
        assert waited and len(urls) == 1
        if urls[0]:
            assert _status_of(urls[0]) == 404  # issued under the old binding, revoked by the rebind
        assert server.seen == []  # the new server is never asked for the old intent's camera
    finally:
        stream_end.set()
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


@pytest.mark.parametrize("change", ["disconnect", "connect_other", "list_without_it", "rebind"])
def test_every_revocation_waits_for_a_host_call_already_admitted(change):
    # Between the admission and the end of the host call no change that voids the stamp may complete (F3).
    rig = Rig()
    rig.connected()
    inside, release = threading.Event(), threading.Event()
    rig.callbacks.set("local_message", lambda *args: (inside.set(), release.wait(4)))
    pusher = threading.Thread(target=lambda: rig.fleet._push(force=True))
    pusher.start()
    assert inside.wait(2)
    if change == "list_without_it":
        rig.server.printers = rig.server.printers[1:]
        answer = rig.fleet._fetch_printers()
    actions = {
        "disconnect": rig.fleet.disconnect,
        "connect_other": lambda: rig.fleet.connect("bamdude-another"),
        "list_without_it": lambda: rig.fleet._deliver_printers(answer),
        "rebind": lambda: rig.fleet.rebind(snapshot(2, base=NEW)),
    }
    changer = threading.Thread(target=actions[change])
    changer.start()
    try:
        time.sleep(0.2)
        assert changer.is_alive() and rig.fleet.target is not None
    finally:
        release.set()
        pusher.join(3)
        changer.join(3)
    assert not changer.is_alive()


# -- the fix2 review: a camera intent never spans a namespace change (R2); a delayed older list (A1) ----------------


def _pause_first(rig, monkeypatch, name, when):
    """Hold the first call of the fleet's or relay's ``name`` for which ``when(args)`` holds; the test releases it."""
    owner = rig.relay if name == "set_target" else rig.fleet
    entered, release, original = threading.Event(), threading.Event(), getattr(owner, name)

    def paused(*args):
        if not entered.is_set() and when(args):
            entered.set()
            assert release.wait(4)
        return original(*args)

    monkeypatch.setattr(owner, name, paused)
    return entered, release


def test_a_namespace_change_waits_for_a_camera_address_being_issued(monkeypatch):
    # Fix2 review R2: an intent formed under the old namespace must not end with the new namespace's admission. The
    # change cannot come between the intent's decision and its answer: it waits, the intent answers under the old
    # secret, and that address dies with the change; the new namespace gets its own.
    rig, relay, opened = _camera_rig(monkeypatch)
    entered, release = _pause_first(rig, monkeypatch, "set_target", lambda args: args[0] is not None)
    issued, worker, changer = [], None, None
    try:
        old = rig.connected()
        worker = threading.Thread(target=lambda: issued.append(rig.fleet.camera_url()))
        worker.start()
        assert entered.wait(2)
        rig.server.epoch = "epoch-after-restore"
        answer = rig.fleet._fetch_status()
        changer = threading.Thread(target=lambda: rig.fleet._deliver_status(answer))
        changer.start()
        time.sleep(0.2)
        assert changer.is_alive()  # the change waits for the address being issued
        release.set()
        worker.join(3)
        changer.join(3)
        assert not changer.is_alive() and rig.fleet.target is None
        (old_url,) = issued
        rig.step()  # the list read again: the new namespace's devices
        new = rig.device_id()
        assert new != old
        rig.fleet.connect(new)
        rig.step()
        new_url = rig.fleet.camera_url()
        assert old_url and new_url and new_url != old_url
        assert _status_of(old_url) == 404 and opened == []
        assert _status_of(new_url) == 200
    finally:
        release.set()
        for thread in (worker, changer):
            if thread is not None:
                thread.join(4)
        relay.stop(2)


def test_a_fence_raised_while_an_address_is_issued_issues_nothing(monkeypatch):
    # The fence is the one change that does not take the publication lock (the unload raises it from its own thread):
    # camera_url looks again after the relay, so an address being issued as it rises is never handed out.
    rig, relay, opened = _camera_rig(monkeypatch)
    entered, release = _pause_first(rig, monkeypatch, "set_target", lambda args: args[0] is not None)
    issued, worker = [], None
    try:
        rig.connected()
        worker = threading.Thread(target=lambda: issued.append(rig.fleet.camera_url()))
        worker.start()
        assert entered.wait(2)
        rig.context.close()
        release.set()
        worker.join(3)
        assert issued == [""] and relay.target is None and opened == []
    finally:
        release.set()
        if worker is not None:
            worker.join(4)
        relay.stop(2)


@pytest.mark.parametrize("change", ["connect", "disconnect"])
def test_a_late_take_down_never_takes_a_newer_camera_admission(monkeypatch, change):
    # A connect or disconnect takes the camera down after its state change; done outside the publication lock, a
    # camera admitted meanwhile -- the same printer, the same target value -- would be taken down with it.
    rig, relay, _ = _camera_rig(monkeypatch)
    changing = []
    entered, release = _pause_first(
        rig, monkeypatch, "_take_camera_down", lambda args: threading.get_ident() in changing
    )
    issued, changer, newer = [], None, None
    try:
        device = rig.connected()
        assert rig.fleet.camera_url()
        action = (lambda: rig.fleet.connect(device)) if change == "connect" else rig.fleet.disconnect

        def run():
            changing.append(threading.get_ident())
            action()

        def reconnect():
            if change == "disconnect":
                rig.fleet.connect(device)
            rig.fleet._deliver_status(rig.fleet._fetch_status())
            issued.append(rig.fleet.camera_url())

        changer = threading.Thread(target=run)
        changer.start()
        assert entered.wait(2)
        newer = threading.Thread(target=reconnect)
        newer.start()
        time.sleep(0.2)
        release.set()
        changer.join(3)
        newer.join(3)
        (url,) = issued
        assert url and _status_of(url) == 200
    finally:
        release.set()
        for thread in (changer, newer):
            if thread is not None:
                thread.join(4)
        relay.stop(2)


def test_the_loops_take_down_never_takes_a_newer_camera_admission(monkeypatch):
    # The loop finds the camera no longer admitted (stale) and takes it down; a camera admitted again in between --
    # the same target value -- must survive it.
    rig, relay, _ = _camera_rig(monkeypatch)
    revoking, entered, release = [], threading.Event(), threading.Event()
    admits = rig.fleet.camera_admits

    def decided_then_paused(target):
        verdict = admits(target)
        if threading.get_ident() in revoking and not verdict and not entered.is_set():
            entered.set()  # the loop has found it no longer admitted and is about to take it down
            assert release.wait(4)
        return verdict

    monkeypatch.setattr(rig.fleet, "camera_admits", decided_then_paused)
    issued, revoker, newer = [], None, None
    try:
        rig.connected()
        assert rig.fleet.camera_url()
        rig.clock.advance(STALE_SECONDS)

        def revoke():
            revoking.append(threading.get_ident())
            rig.fleet._revoke_camera()

        def fresh_again():
            rig.fleet._deliver_status(rig.fleet._fetch_status())
            issued.append(rig.fleet.camera_url())

        revoker = threading.Thread(target=revoke)
        revoker.start()
        assert entered.wait(2)
        newer = threading.Thread(target=fresh_again)
        newer.start()
        time.sleep(0.2)
        release.set()
        revoker.join(3)
        newer.join(3)
        (url,) = issued
        assert url and _status_of(url) == 200
    finally:
        release.set()
        for thread in (revoker, newer):
            if thread is not None:
                thread.join(4)
        relay.stop(2)


def test_a_delayed_list_of_an_older_namespace_does_not_bring_it_back():
    # Fix2 review A1: the poller lets a read go before its answer is delivered, so a newer list can be read and
    # delivered while an older one still waits; the era keeps the older one out (Codex's reproduction).
    rig = Rig()
    old = rig.connected()
    channel = rig.fleet.poller._channels["printers"]
    deliver, delivered = channel.deliver, []
    entered, release = threading.Event(), threading.Event()

    def delayed(value):
        delivered.append(1)
        if len(delivered) == 1:
            entered.set()
            assert release.wait(4)
        deliver(value)

    channel.deliver = delayed
    rig.fleet.want_fleet(True)
    rig.fleet.tick()
    worker = threading.Thread(target=rig.jobs.queued.pop())
    worker.start()
    try:
        assert entered.wait(2) and not channel.in_flight
        rig.server.epoch = "epoch-after-restore"
        new = rig.device_id()
        rig.fleet.discover()
        rig.fleet.tick()
        rig.jobs.run_all()
        assert rig.fleet.devices.get(new) is not None
        rig.take()
        release.set()
        worker.join(3)
        assert rig.fleet.devices.get(new) is not None and rig.fleet.devices.get(old) is None
        assert rig.take("ssdp") == []
    finally:
        release.set()
        worker.join(4)
