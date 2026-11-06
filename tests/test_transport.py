"""The transport against a real local HTTP server: what is sent, what is refused, what is never followed."""

import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import pytest
from fakes import Server
from fakes import json_answer as _json

from orca_bamdude.config import Connection
from orca_bamdude.transport import MAX_JSON_RESPONSE, HttpError, Transport, TransportError, _ssl_context


def test_a_request_carries_the_key_and_the_proxy_headers_to_the_base_path(serve):
    server, transport = serve(lambda m, p: _json(200, {"ok": True}), extra_headers=[("CF-Access-Client-Id", "abc")])

    status, body = transport.request_json("GET", "/api/v1/connector/capabilities")

    assert (status, body) == (200, {"ok": True})
    seen = server.seen[0]
    assert seen["path"] == "/base/api/v1/connector/capabilities"
    assert seen["headers"]["X-API-Key"] == "bd_test_key"
    assert seen["headers"]["CF-Access-Client-Id"] == "abc"


def test_a_redirect_is_never_followed(serve):
    other, _ = serve(lambda m, p: _json(200, {"followed": True}))
    server, transport = serve(lambda m, p: (302, {"Location": other.url + "/api/v1/x"}, b""))

    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/connector/capabilities")

    assert refused.value.kind == "redirect"
    assert other.seen == []


def test_a_refusal_carries_only_the_servers_code_and_sentence(serve):
    detail = {"detail": {"error": "operation_conflict", "message": "Already submitted."}}
    _server, transport = serve(lambda m, p: _json(409, detail, **{"Retry-After": "7"}))

    with pytest.raises(HttpError) as refused:
        transport.request_json("POST", "/api/v1/connector/submissions", body={"a": 1})

    assert (refused.value.status, refused.value.code, refused.value.message) == (
        409,
        "operation_conflict",
        "Already submitted.",
    )
    assert refused.value.retry_after == 7.0


def test_an_html_error_page_is_never_shown(serve):
    _server, transport = serve(lambda m, p: (502, {"Content-Type": "text/html"}, b"<html>secret proxy page</html>"))

    with pytest.raises(HttpError) as refused:
        transport.request_json("GET", "/api/v1/printers/")

    assert refused.value.code == "http_error" and refused.value.message == ""


def test_a_non_json_success_is_a_protocol_error(serve):
    _server, transport = serve(lambda m, p: (200, {"Content-Type": "text/html"}, b"<html>login</html>"))

    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/printers/")

    assert refused.value.kind == "protocol"


def test_an_oversized_answer_is_refused(serve):
    big = b'{"x":"' + b"a" * (MAX_JSON_RESPONSE + 10) + b'"}'
    _server, transport = serve(lambda m, p: (200, {"Content-Type": "application/json"}, big))

    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/printers/")

    assert refused.value.kind == "too_large"


def test_an_upload_streams_the_file_as_the_raw_body(serve, tmp_path):
    server, transport = serve(lambda m, p: _json(200, {"id": 7, "outcome": "created"}))
    payload = b"PK\x03\x04" + b"x" * 700_000
    source = tmp_path / "part.gcode.3mf"
    source.write_bytes(payload)
    progress = []

    status, body = transport.upload_file(
        "/api/v1/connector/files",
        str(source),
        len(payload),
        query={"filename": "part.gcode.3mf"},
        progress=lambda sent, total: progress.append(sent),
    )

    assert (status, body["id"]) == (200, 7)
    seen = server.seen[0]
    assert seen["body"] == payload
    assert seen["headers"]["Content-Type"] == "application/octet-stream"
    assert seen["path"] == "/base/api/v1/connector/files?filename=part.gcode.3mf"
    assert progress[-1] == len(payload)


def test_a_cancelled_upload_stops_between_chunks(serve, tmp_path):
    _server, transport = serve(lambda m, p: _json(200, {}))
    source = tmp_path / "part.gcode.3mf"
    source.write_bytes(b"x" * 2_000_000)

    with pytest.raises(TransportError) as refused:
        transport.upload_file("/api/v1/connector/files", str(source), 2_000_000, cancelled=lambda: True)

    assert refused.value.kind == "cancelled"


def test_nothing_outside_the_api_is_requested(serve):
    _server, transport = serve(lambda m, p: _json(200, {}))

    with pytest.raises(ValueError):
        transport.request_json("GET", "https://elsewhere.example/api/v1/x")


def test_an_unreachable_server_is_a_connect_error():
    transport = Transport(Connection.build("http://127.0.0.1:9", "bd_k"), timeout=2)

    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/printers/")

    assert refused.value.kind in ("connect", "timeout")


MJPEG = "multipart/x-mixed-replace; boundary=frame"


def test_a_stream_carries_the_key_and_the_printer_identity(serve):
    server, transport = serve(lambda m, p: (200, {"Content-Type": MJPEG}, iter([b"--frame\r\n"])))

    stream = transport.open_stream(
        "/api/v1/printers/7/camera/stream", headers={"X-BamDude-Printer-Identity": "uuid-7"}, query={"fps": 5}
    )
    try:
        assert stream.content_type == MJPEG
        assert stream.read(1024) == b"--frame\r\n"
    finally:
        stream.close()

    seen = server.seen[0]
    assert seen["path"] == "/base/api/v1/printers/7/camera/stream?fps=5"
    assert seen["headers"]["X-API-Key"] == "bd_test_key"
    assert seen["headers"]["X-BamDude-Printer-Identity"] == "uuid-7"


def test_closing_a_stream_from_another_thread_ends_a_waiting_read(serve):
    release = threading.Event()

    def body():
        yield b"--frame\r\n"
        release.wait(5)

    _server, transport = serve(lambda m, p: (200, {"Content-Type": MJPEG}, body()))
    stream = transport.open_stream("/api/v1/printers/7/camera/stream")
    try:
        assert stream.read(1024) == b"--frame\r\n"
        threading.Timer(0.2, stream.close).start()
        started = time.monotonic()

        assert stream.read(1024) == b""
        assert time.monotonic() - started < 3
    finally:
        release.set()
        stream.close()


def test_a_refused_stream_is_the_servers_refusal(serve):
    detail = {"detail": {"error": "printer_scope", "message": "Not this printer."}}
    _server, transport = serve(lambda m, p: _json(403, detail))

    with pytest.raises(HttpError) as refused:
        transport.open_stream("/api/v1/printers/7/camera/stream")

    assert (refused.value.status, refused.value.code) == (403, "printer_scope")


def test_a_redirected_stream_is_never_followed(serve):
    _server, transport = serve(lambda m, p: (307, {"Location": "http://127.0.0.1:9/x"}, b""))

    with pytest.raises(TransportError) as refused:
        transport.open_stream("/api/v1/printers/7/camera/stream")

    assert refused.value.kind == "redirect"


TLS = Path(__file__).parent / "data" / "tls"


def _tls_server():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(TLS / "server.pem", TLS / "server-key.pem")
    return Server(lambda m, p: _json(200, {"ok": True}), tls=context)


@pytest.mark.parametrize("ca", [None, "ca.pem"])
def test_tls_always_verifies(ca):
    context = _ssl_context((TLS / ca).read_text(encoding="ascii") if ca else None)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_a_pasted_ca_verifies_the_server_it_signed():
    server = _tls_server()
    try:
        connection = Connection.build(
            f"https://localhost:{server.port}/base", "bd_k", ca_pem=(TLS / "ca.pem").read_text(encoding="ascii")
        )
        assert Transport(connection, timeout=5).request_json("GET", "/api/v1/x") == (200, {"ok": True})
    finally:
        server.close()


@pytest.mark.parametrize(
    "host,ca",
    [
        ("localhost", "foreign-ca.pem"),  # signed by another CA
        ("127.0.0.1", "ca.pem"),  # the right CA, but the certificate names only localhost
        ("localhost", None),  # the system's trust does not know a private CA
    ],
)
def test_a_server_the_ca_does_not_vouch_for_is_refused(host, ca):
    server = _tls_server()
    try:
        pem = (TLS / ca).read_text(encoding="ascii") if ca else None
        connection = Connection.build(f"https://{host}:{server.port}/base", "bd_k", ca_pem=pem)
        with pytest.raises(TransportError) as refused:
            Transport(connection, timeout=5).request_json("GET", "/api/v1/x")
        assert refused.value.kind == "tls"
        assert not server.seen  # nothing, the key included, was sent before the handshake failed
    finally:
        server.close()


# -- the owner cancels what is in flight (H2 review R3) ------------------------------------------------------------


def _held(release):
    def routes(method, path):
        release.wait(10)
        return _json(200, {"late": True})

    return routes


def test_close_ends_a_request_waiting_for_its_answer(serve):
    release = threading.Event()
    server, transport = serve(_held(release))
    outcome = {}

    def ask():
        try:
            outcome["value"] = transport.request_json("GET", "/api/v1/connector/capabilities")
        except TransportError as exc:
            outcome["kind"] = exc.kind

    worker = threading.Thread(target=ask)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.seen and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        transport.close()
        worker.join(3)
        assert not worker.is_alive() and time.monotonic() - started < 2
        assert outcome == {"kind": "cancelled"}
    finally:
        release.set()
        worker.join(5)


def test_after_close_nothing_is_sent(serve):
    server, transport = serve(lambda m, p: _json(200, {}))
    transport.close()
    for call in (
        lambda: transport.request_json("GET", "/api/v1/connector/capabilities"),
        lambda: transport.open_stream("/api/v1/printers/1/camera/stream"),
    ):
        with pytest.raises(TransportError) as refused:
            call()
        assert refused.value.kind == "cancelled"
    assert server.seen == []


def test_close_ends_a_stream_still_waiting_for_its_headers(serve):
    release = threading.Event()
    server, transport = serve(_held(release))
    outcome = {}

    def open_it():
        try:
            outcome["stream"] = transport.open_stream("/api/v1/printers/1/camera/stream")
        except TransportError as exc:
            outcome["kind"] = exc.kind

    worker = threading.Thread(target=open_it)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.seen and time.monotonic() < deadline:
            time.sleep(0.01)
        transport.close()
        worker.join(3)
        assert not worker.is_alive() and outcome == {"kind": "cancelled"}
    finally:
        release.set()
        worker.join(5)


def test_a_stream_already_handed_over_is_its_owners_to_close(serve):
    def routes(method, path):
        return 200, {"Content-Type": "multipart/x-mixed-replace; boundary=f"}, iter([b"--f\r\n", b"x" * 10])

    server, transport = serve(routes)
    stream = transport.open_stream("/api/v1/printers/1/camera/stream")
    transport.close()  # the stream is the relay's now: closing the transport does not pull it from under it
    assert stream.read(4)
    stream.close()


def test_a_closed_transport_does_not_even_connect(serve, monkeypatch):
    server, transport = serve(lambda m, p: _json(200, {}))
    connects = []
    real = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: (connects.append(1), real(self, address))[1])
    transport.close()
    with pytest.raises(TransportError):
        transport.request_json("GET", "/api/v1/connector/capabilities")
    assert connects == []


def test_a_close_before_the_socket_connects_sends_nothing(serve, monkeypatch):
    # The socket is the transport's from its creation: a close that lands before it connects still owns it.
    server, transport = serve(lambda m, p: _json(200, {}))
    real = socket.socket.connect

    def close_then_connect(self, address):
        transport.close()
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", close_then_connect)
    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/connector/capabilities")
    assert refused.value.kind == "cancelled"
    time.sleep(0.2)
    assert server.seen == []


def test_the_connection_is_made_by_the_granted_host_name(serve, monkeypatch):
    # OrcaSlicer matches a socket.connect outside http.client's own frames against its target: the transport connects
    # by the configured host, never by an address it resolved, so the target stays the host the user granted.
    server, _ = serve(lambda m, p: _json(200, {}))
    transport = Transport(Connection.build(f"http://localhost:{server.port}/base", "bd_k"), timeout=5)
    targets, audited = [], []
    real = socket.socket.connect

    def connect(self, address):
        targets.append(address[:2])
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    hook_events = {"http.client.connect", "socket.connect"}

    def hook(event, args):
        if event in hook_events:
            audited.append((event, args[1][:2] if event == "socket.connect" else (args[1], args[2])))

    sys.addaudithook(hook)
    try:
        try:
            transport.request_json("GET", "/api/v1/connector/capabilities")
        except TransportError:
            pass  # the server may listen on IPv4 only; what was asked for is what counts
    finally:
        hook_events.clear()  # an audit hook cannot be removed; it goes quiet
    assert targets and all(address == ("localhost", server.port) for address in targets)
    assert ("http.client.connect", ("localhost", server.port)) in audited
    assert all(target == ("localhost", server.port) for event, target in audited if event == "socket.connect")


def _raw_server(answer):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    release = threading.Event()
    started = threading.Event()

    def serve():
        conn, _ = listener.accept()
        with conn:
            started.set()
            answer(conn, release)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, release, started, thread


@pytest.mark.parametrize("connection", ["close", "keep-alive"])
def test_close_ends_a_body_still_arriving(connection):
    # After the headers, http.client hands a Connection: close socket to the response and forgets it; the transport
    # still owns it (H2 fix review F1).
    def answer(conn, release):
        conn.recv(16384)
        head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: "
        conn.sendall((head + connection + "\r\n\r\n").encode())
        release.wait(6)
        try:
            conn.sendall(b"{}")
        except OSError:
            pass

    listener, release, started, thread = _raw_server(answer)
    port = listener.getsockname()[1]
    transport = Transport(Connection.build(f"http://127.0.0.1:{port}", "bd_synthetic"), timeout=8)
    reading, outcome = threading.Event(), {}
    real = transport._json

    def json_read(response):
        reading.set()
        return real(response)

    transport._json = json_read

    def ask():
        try:
            outcome["value"] = transport.request_json("GET", "/api/v1/connector/capabilities")
        except TransportError as exc:
            outcome["kind"] = exc.kind

    worker = threading.Thread(target=ask)
    worker.start()
    try:
        assert reading.wait(3)
        transport.close()
        worker.join(2)
        alive = worker.is_alive()
        release.set()
        worker.join(5)
        assert not alive and outcome == {"kind": "cancelled"}
    finally:
        release.set()
        worker.join(9)
        thread.join(9)
        listener.close()


def test_close_ends_a_tls_handshake_in_progress():
    def answer(conn, release):
        conn.recv(16384)  # the real ClientHello; no ServerHello comes
        release.wait(6)

    listener, release, started, thread = _raw_server(answer)
    port = listener.getsockname()[1]
    transport = Transport(Connection.build(f"https://127.0.0.1:{port}", "bd_synthetic"), timeout=8)
    outcome = {}

    def ask():
        try:
            transport.request_json("GET", "/api/v1/connector/capabilities")
        except TransportError as exc:
            outcome["kind"] = exc.kind

    worker = threading.Thread(target=ask)
    worker.start()
    try:
        assert started.wait(3)
        time.sleep(0.2)  # inside the handshake
        transport.close()
        worker.join(2)
        alive = worker.is_alive()
        release.set()
        worker.join(5)
        assert not alive and outcome == {"kind": "cancelled"}
    finally:
        release.set()
        worker.join(9)
        thread.join(9)
        listener.close()


def test_an_answer_read_after_close_is_not_returned(serve, monkeypatch):
    # A close that lands while the body is already in: the answer was read, but the owner said stop.
    server, transport = serve(lambda m, p: _json(200, {"late": True}))
    real = transport._json

    def read_then_closed(response):
        value = real(response)
        transport.close()
        return value

    transport._json = read_then_closed
    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/connector/capabilities")
    assert refused.value.kind == "cancelled"
