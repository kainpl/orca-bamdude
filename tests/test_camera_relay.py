"""The loopback camera relay: what it serves, to whom, and when it lets go (spec r2 §11)."""

import http.client
import socket
import threading
import time

import pytest

from orca_bamdude.camera_relay import MAX_CONNECTIONS, CameraRelay, Target
from orca_bamdude.transport import HttpError, TransportError

MJPEG = "multipart/x-mixed-replace; boundary=frame"
FRAME = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xff\xe0jpegdata\xff\xd9\r\n"
PRINTER = Target(printer_id=7, identity="11111111-2222-3333-4444-555555555555")
OTHER = Target(printer_id=8, identity="99999999-2222-3333-4444-555555555555")


class FakeUpstream:
    """An upstream camera stream: its chunks, then either the end or a wait until it is closed."""

    def __init__(self, chunks=(FRAME,), *, content_type=MJPEG, hold=True):
        self.content_type = content_type
        self._chunks = list(chunks)
        self._hold = hold
        self._closed = threading.Event()

    def read(self, size):
        if self._chunks:
            return self._chunks.pop(0)
        if self._hold:
            self._closed.wait(10)
        return b""

    def close(self):
        self._closed.set()

    @property
    def closed(self):
        return self._closed.is_set()


class Opener:
    """Hands out the scripted upstreams in order and records which targets were asked for."""

    def __init__(self, *upstreams):
        self.upstreams = list(upstreams)
        self.targets = []

    def __call__(self, target):
        self.targets.append(target)
        item = self.upstreams.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def relays():
    started = []

    def make(opener):
        events = []
        relay = CameraRelay(opener, on_event=events.append)
        relay.start()
        started.append(relay)
        return relay, events

    yield make
    for relay in started:
        relay.stop()


def _get(url, *, host=None, method="GET"):
    parts = url.split("/", 3)
    authority, path = parts[2], "/" + parts[3]
    hostname, port = authority.rsplit(":", 1)
    conn = http.client.HTTPConnection(hostname, int(port), timeout=5)
    if host is None:
        conn.request(method, path)
    else:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host)
        conn.endheaders()
    return conn, conn.getresponse()


def _leave(conn, response):
    """A player that goes away: the response holds the socket, so both are closed."""
    response.close()
    conn.close()


def test_the_relay_listens_on_loopback_behind_a_long_secret(relays):
    relay, _ = relays(Opener())
    relay.set_target(PRINTER)

    url = relay.url_for(PRINTER)

    assert url.startswith("http://127.0.0.1:")
    secret = url.split("/")[3]
    assert len(secret) >= 32
    assert url.endswith("/" + PRINTER.identity)
    other, _ = relays(Opener())
    assert other.url_for(PRINTER) is None  # no target chosen yet
    other.set_target(PRINTER)
    assert other.url_for(PRINTER).split("/")[3] != secret


def test_a_viewer_gets_the_upstream_bytes_and_a_frame_is_reported(relays):
    upstream = FakeUpstream()
    opener = Opener(upstream)
    relay, events = relays(opener)
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER))
    try:
        assert response.status == 200
        assert response.getheader("Content-Type") == MJPEG
        assert response.read(len(FRAME)) == FRAME
        assert _wait_for(lambda: any(e.kind == "frame" for e in events))
    finally:
        _leave(conn, response)

    assert opener.targets == [PRINTER]
    assert _wait_for(lambda: upstream.closed)


@pytest.mark.parametrize("variant", ["secret", "identity", "query", "host", "extra"])
def test_anything_but_the_exact_address_is_not_found(relays, variant):
    opener = Opener(FakeUpstream())
    relay, _ = relays(opener)
    relay.set_target(PRINTER)
    url = relay.url_for(PRINTER)
    port = url.split(":")[2].split("/")[0]
    host = None
    if variant == "secret":
        url = url.replace(url.split("/")[3], "x" * 43)
    elif variant == "identity":
        url = url.replace(PRINTER.identity, OTHER.identity)
    elif variant == "query":
        url += "?fps=30"
    elif variant == "host":
        host = f"localhost:{port}"
    elif variant == "extra":
        url += "/more"

    conn, response = _get(url, host=host)
    try:
        assert response.status == 404
        assert PRINTER.identity not in response.read().decode("latin-1")
    finally:
        conn.close()
    assert opener.targets == []


def test_only_get_is_served(relays):
    relay, _ = relays(Opener(FakeUpstream()))
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER), method="POST")
    conn.close()

    assert response.status == 405


def test_a_refused_upstream_is_a_bad_gateway_and_one_unavailable_event(relays):
    refusal = HttpError(403, "printer_scope", "This key may not reach that printer.")
    relay, events = relays(Opener(refusal))
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER))
    body = response.read()
    conn.close()

    assert response.status == 502
    assert b"printer_scope" not in body and b"may not reach" not in body
    unavailable = [e for e in events if e.kind == "unavailable"]
    assert [(e.code, e.target) for e in unavailable] == [("printer_scope", PRINTER)]


def test_an_upstream_that_is_not_mjpeg_is_refused(relays):
    upstream = FakeUpstream([b"<html>login</html>"], content_type="text/html")
    relay, events = relays(Opener(upstream))
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER))
    body = response.read()
    conn.close()

    assert response.status == 502 and b"login" not in body
    assert [e.code for e in events if e.kind == "unavailable"] == ["not_mjpeg"]
    assert upstream.closed


def test_a_stream_that_ends_without_a_picture_is_no_picture(relays):
    relay, events = relays(Opener(FakeUpstream([b"--frame\r\n\r\n"], hold=False)))
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER))
    response.read()
    conn.close()

    assert _wait_for(lambda: any(e.kind == "unavailable" for e in events))
    assert [e.code for e in events if e.kind == "unavailable"] == ["no_picture"]
    assert not any(e.kind == "frame" for e in events)


def test_a_newer_viewer_replaces_the_older_one(relays):
    first, second = FakeUpstream(), FakeUpstream()
    relay, _ = relays(Opener(first, second))
    relay.set_target(PRINTER)
    url = relay.url_for(PRINTER)

    conn1, response1 = _get(url)
    assert response1.read(len(FRAME)) == FRAME
    conn2, response2 = _get(url)
    try:
        assert response2.read(len(FRAME)) == FRAME
        assert response1.read() == b""  # the older viewer is let go
        assert _wait_for(lambda: first.closed)
        assert not second.closed
    finally:
        conn1.close()
        conn2.close()


def test_an_ended_lease_is_renewed_under_the_same_viewer(relays):
    renewed = FakeUpstream([FRAME + b"second"])
    opener = Opener(FakeUpstream([FRAME], hold=False), renewed)
    relay, _ = relays(opener)
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER))
    try:
        assert response.read(len(FRAME)) == FRAME
        assert response.read(len(FRAME) + 6) == FRAME + b"second"
    finally:
        conn.close()

    assert opener.targets == [PRINTER, PRINTER]


def test_a_renewal_that_is_refused_ends_the_viewer(relays):
    opener = Opener(FakeUpstream([FRAME], hold=False), HttpError(401, "invalid_api_key", "Invalid API key."))
    relay, events = relays(opener)
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER))
    try:
        assert response.read(len(FRAME)) == FRAME
        assert response.read() == b""
    finally:
        conn.close()

    assert [e.code for e in events if e.kind == "unavailable"] == ["invalid_api_key"]
    assert opener.targets == [PRINTER, PRINTER]


class StallingUpstream(FakeUpstream):
    """Delivers a picture, then its read times out: a camera that stopped sending."""

    def read(self, size):
        if self._chunks:
            return self._chunks.pop(0)
        raise TransportError("timeout")


def test_a_stalled_stream_ends_the_viewer_without_a_renewal(relays):
    opener = Opener(StallingUpstream([FRAME]), FakeUpstream())
    relay, events = relays(opener)
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER))
    try:
        assert response.read(len(FRAME)) == FRAME
        assert response.read() == b""
    finally:
        conn.close()

    assert [e.code for e in events if e.kind == "unavailable"] == ["timeout"]
    assert opener.targets == [PRINTER]


def test_a_player_that_leaves_a_silent_camera_is_noticed(relays):
    upstream = FakeUpstream()  # one picture, then silence
    relay, events = relays(Opener(upstream))
    relay.set_target(PRINTER)

    conn, response = _get(relay.url_for(PRINTER))
    assert response.read(len(FRAME)) == FRAME
    _leave(conn, response)

    assert _wait_for(lambda: upstream.closed, timeout=3)
    assert _wait_for(lambda: any(e.kind == "ended" for e in events), timeout=3)


def test_a_new_target_lets_the_old_viewer_go_and_closes_its_address(relays):
    upstream = FakeUpstream()
    relay, _ = relays(Opener(upstream))
    relay.set_target(PRINTER)
    old_url = relay.url_for(PRINTER)
    conn, response = _get(old_url)
    assert response.read(len(FRAME)) == FRAME

    relay.set_target(OTHER)

    assert response.read() == b""
    assert _wait_for(lambda: upstream.closed)
    conn.close()
    assert relay.url_for(PRINTER) is None
    conn2, response2 = _get(old_url)
    conn2.close()
    assert response2.status == 404


def test_rotating_the_secret_closes_the_viewer_and_the_old_address(relays):
    upstream = FakeUpstream()
    relay, _ = relays(Opener(upstream))
    relay.set_target(PRINTER)
    old_url = relay.url_for(PRINTER)
    conn, response = _get(old_url)
    assert response.read(len(FRAME)) == FRAME

    relay.rotate()

    assert response.read() == b""
    conn.close()
    assert relay.url_for(PRINTER) != old_url
    conn2, response2 = _get(old_url)
    conn2.close()
    assert response2.status == 404


def test_connections_beyond_the_cap_are_dropped_without_a_handler(relays):
    opener = Opener()
    relay, _ = relays(opener)
    relay.set_target(PRINTER)
    port = int(relay.url_for(PRINTER).split(":")[2].split("/")[0])
    idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(MAX_CONNECTIONS)]
    try:
        assert _wait_for(lambda: relay.active_connections == MAX_CONNECTIONS)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            assert extra.recv(1) == b""
        finally:
            extra.close()
        assert relay.active_connections == MAX_CONNECTIONS
    finally:
        for sock in idle:
            sock.close()
    assert opener.targets == []


def test_stop_lets_every_viewer_go_and_joins_its_threads(relays):
    upstream = FakeUpstream()
    relay, _ = relays(Opener(upstream))
    relay.set_target(PRINTER)
    conn, response = _get(relay.url_for(PRINTER))
    assert response.read(len(FRAME)) == FRAME

    started = time.monotonic()
    assert relay.stop() is True
    assert time.monotonic() - started < 5

    assert response.read() == b""
    conn.close()
    assert upstream.closed
    assert relay.url_for(PRINTER) is None
    assert relay.active_connections == 0


def test_the_secret_never_reaches_a_log(relays, capsys):
    relay, _ = relays(Opener(FakeUpstream()))
    relay.set_target(PRINTER)
    url = relay.url_for(PRINTER)

    conn, response = _get(url)
    response.read(len(FRAME))
    conn.close()
    conn2, response2 = _get(url + "?x=1")
    conn2.close()
    relay.stop()

    captured = capsys.readouterr()
    assert url.split("/")[3] not in captured.err + captured.out


# -- admission: asked at the request, at every open and at every renewal (H2 review R1) --------------------------


@pytest.fixture
def admitted_relays():
    started = []

    def make(opener, admits):
        events = []
        relay = CameraRelay(opener, on_event=events.append, admits=admits)
        relay.start()
        started.append(relay)
        return relay, events

    yield make
    for relay in started:
        relay.stop()


def test_a_target_no_longer_admitted_is_not_found_and_opens_nothing(admitted_relays):
    allowed = {"now": True}
    opener = Opener(FakeUpstream())
    relay, _ = admitted_relays(opener, lambda target: allowed["now"])
    relay.set_target(PRINTER)
    url = relay.url_for(PRINTER)
    allowed["now"] = False  # stale, disconnected, another binding, the right lost, the fence
    conn, response = _get(url)
    try:
        assert response.status == 404
    finally:
        _leave(conn, response)
    assert opener.targets == []


def test_a_renewal_asks_for_admission_again(admitted_relays):
    allowed = {"now": True}
    first = FakeUpstream([FRAME], hold=True)  # this lease ends only when the test says so
    opener = Opener(first, FakeUpstream([FRAME + b"second"]))
    relay, events = admitted_relays(opener, lambda target: allowed["now"])
    relay.set_target(PRINTER)
    conn, response = _get(relay.url_for(PRINTER))
    try:
        assert response.read(len(FRAME)) == FRAME
        allowed["now"] = False  # lost while the first lease played
        first.close()  # the server ends the lease
        assert response.read() == b""  # and it is not renewed
    finally:
        conn.close()
    assert opener.targets == [PRINTER]
    assert [e.code for e in events if e.kind == "unavailable"] == ["not_admitted"]


def test_an_admitted_renewal_still_works(admitted_relays):
    opener = Opener(FakeUpstream([FRAME], hold=False), FakeUpstream([FRAME + b"second"]))
    relay, _ = admitted_relays(opener, lambda target: True)
    relay.set_target(PRINTER)
    conn, response = _get(relay.url_for(PRINTER))
    try:
        assert response.read(len(FRAME)) == FRAME
        assert response.read(len(FRAME) + 6) == FRAME + b"second"
    finally:
        conn.close()
    assert opener.targets == [PRINTER, PRINTER]


def test_the_current_target_is_readable(relays):
    relay, _ = relays(Opener())
    assert relay.target is None
    relay.set_target(PRINTER)
    assert relay.target == PRINTER
