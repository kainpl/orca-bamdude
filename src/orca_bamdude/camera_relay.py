"""The loopback relay the slicer's camera player reads from (spec r2 §11).

OrcaSlicer plays a printer agent's camera with its own FFmpeg, which sends no request headers, so it cannot carry
the API key, the proxy headers or the printer identity, and a camera URL that changes stops a playing stream. The
relay therefore listens on 127.0.0.1 and hands FFmpeg a fixed address; for each viewer it opens the farm's stream
through the plugin's own client, where every credential stays.

- Bound to 127.0.0.1 on a port the OS picks; the socket is made on the relay's own thread, never the caller's.
- The address is ``/<secret>/<printer identity>``: a per-relay secret of 256 bits, compared in constant time, and
  the identity of the one printer the plugin chose. Anything else -- another secret, another printer, a query, a
  further segment, a Host header other than this relay's -- is 404 and opens nothing. The relay is no proxy: the
  upstream it opens is decided by the plugin, never by the request.
- One viewer at a time; a newer viewer lets the older one go. A new target, a new secret (``rotate``) and ``stop``
  let the viewer go too, closing its upstream and its socket.
- A server stream for an API key lasts at most a lease; when one that delivered a picture ends, the relay opens the
  next under the same viewer, which is where the server checks the key again. A refused renewal ends the viewer.
- What failed is reported as an event with a stable code, never by forwarding the server's answer to the player,
  and nothing is logged with the address in it: the player gets a bare 502.
- Connections are capped; one beyond the cap is closed without a thread. ``stop`` joins every thread it started,
  with a deadline, and says whether they all ended.
"""

from __future__ import annotations

import http.server
import secrets
import select
import socket
import socketserver
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from orca_bamdude.transport import HttpError, TransportError

HOST = "127.0.0.1"
MAX_CONNECTIONS = 4
CHUNK = 64 * 1024
REQUEST_TIMEOUT = 10.0
JOIN_SECONDS = 5.0
WATCH_SECONDS = 0.25
MJPEG = "multipart/x-mixed-replace"
_JPEG_START = b"\xff\xd8"


@dataclass(frozen=True)
class Target:
    printer_id: int
    identity: str


@dataclass(frozen=True)
class RelayEvent:
    """``frame``: the first picture reached the viewer. ``unavailable``: the camera could not be shown, ``code`` says
    why (the server's refusal code, a transport kind, ``not_mjpeg`` or ``no_picture``). ``ended``: the viewer left."""

    kind: str
    target: Target
    code: str = ""


class Upstream(Protocol):
    content_type: str

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class RelayError(Exception):
    pass


class _Viewer:
    def __init__(self, target: Target, sock: socket.socket):
        self.target = target
        self.sock = sock
        self.upstream: Upstream | None = None
        self.released = False

    def release(self) -> None:
        """Let this viewer go: its upstream and its player socket are closed, from whichever thread asks."""
        self.released = True
        upstream, self.upstream = self.upstream, None
        if upstream is not None:
            try:
                upstream.close()
            except Exception:
                pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class CameraRelay:
    """``admits(target)``, when given, is asked at every request, every upstream open and every renewal: a target its
    owner no longer admits (stale, disconnected, another binding, the right lost, the fence) is not found and opens
    nothing, and a lease that ends under it is not renewed (H2 review R1). Admission is the owner's; the relay never
    keeps one of its own."""

    def __init__(
        self,
        opener: Callable[[Target], Upstream],
        on_event: Callable[[RelayEvent], None] | None = None,
        admits: Callable[[Target], bool] | None = None,
    ):
        self._opener = opener
        self._on_event = on_event
        self._admits = admits
        self._lock = threading.Lock()
        self._secret = secrets.token_urlsafe(32)
        self._target: Target | None = None
        self._viewer: _Viewer | None = None
        self._threads: set[threading.Thread] = set()
        self._watchers: set[threading.Thread] = set()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle

    def start(self, timeout: float = JOIN_SECONDS) -> None:
        """Bind and serve on the relay's own thread; returns once the port is known."""
        ready = threading.Event()
        failure: list[BaseException] = []

        def run() -> None:
            try:
                server = _Server(self)
            except OSError as exc:
                failure.append(exc)
                ready.set()
                return
            with self._lock:
                self._server = server
            ready.set()
            server.serve_forever(poll_interval=0.2)

        self._thread = threading.Thread(target=run, name="orca-bamdude-camera-relay", daemon=True)
        self._thread.start()
        if not ready.wait(timeout) or failure:
            raise RelayError("bind_failed")

    def stop(self, timeout: float = JOIN_SECONDS) -> bool:
        """Let every viewer go, stop serving and join every thread; ``True`` only if they all ended in time."""
        with self._lock:
            self._stopping = True
            server, self._server = self._server, None
            viewer, self._viewer = self._viewer, None
            self._target = None
        if viewer is not None:
            viewer.release()
        if server is not None:
            server.shutdown()
            server.server_close()
        threads = [self._thread] if self._thread is not None else []
        with self._lock:
            threads += list(self._threads) + list(self._watchers)
        for thread in threads:
            thread.join(timeout)
        return not any(thread.is_alive() for thread in threads)

    # ------------------------------------------------------------------ what the plugin sets

    def set_target(self, target: Target | None) -> None:
        """Choose the printer whose camera is served; a different one lets the current viewer go."""
        with self._lock:
            if target == self._target:
                return
            self._target = target
            viewer, self._viewer = self._viewer, None
        if viewer is not None:
            viewer.release()

    def rotate(self) -> None:
        """A new secret (reconnect, changed settings, disable): the old address stops working at once."""
        with self._lock:
            self._secret = secrets.token_urlsafe(32)
            viewer, self._viewer = self._viewer, None
        if viewer is not None:
            viewer.release()

    @property
    def target(self) -> Target | None:
        with self._lock:
            return self._target

    def _admitted(self, target: Target) -> bool:
        if self._admits is None:
            return True
        try:
            return bool(self._admits(target))
        except Exception:  # noqa: BLE001 -- an admission that cannot be decided admits nothing
            return False

    def url_for(self, target: Target) -> str | None:
        """The player's address for ``target``, or ``None`` while it is not the chosen printer or nothing listens."""
        with self._lock:
            if self._server is None or self._stopping or target != self._target:
                return None
            return f"http://{HOST}:{self._server.server_port}/{self._secret}/{target.identity}"

    @property
    def active_connections(self) -> int:
        with self._lock:
            return len(self._threads)

    # ------------------------------------------------------------------ serving

    def _emit(self, kind: str, target: Target, code: str = "") -> None:
        if self._on_event is not None:
            try:
                self._on_event(RelayEvent(kind, target, code))
            except Exception:
                pass

    def _accept(self, serve: Callable[[], None]) -> bool:
        with self._lock:
            if self._stopping or len(self._threads) >= MAX_CONNECTIONS:
                return False
            thread = threading.Thread(target=self._run_connection, args=(serve,), daemon=True)
            self._threads.add(thread)
        thread.start()
        return True

    def _run_connection(self, serve: Callable[[], None]) -> None:
        try:
            serve()
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _resolve(self, host: str | None, raw_path: str, port: int) -> Target | None:
        """The target this exact request addresses, or ``None``."""
        if host != f"{HOST}:{port}":
            return None
        parts = urlsplit(raw_path)
        if parts.query or parts.fragment or raw_path != parts.path:
            return None
        segments = parts.path.split("/")
        if len(segments) != 3 or segments[0] != "":
            return None
        with self._lock:
            secret, target = self._secret, self._target
        if target is None or self._stopping:
            return None
        if not secrets.compare_digest(segments[1].encode(), secret.encode()):
            return None
        if segments[2] != target.identity:
            return None
        if not self._admitted(target):
            return None
        return target

    def _claim(self, target: Target, sock: socket.socket) -> _Viewer | None:
        """Make this connection the viewer of ``target``; the previous viewer is let go."""
        with self._lock:
            if self._stopping or self._target != target:
                return None
            previous, viewer = self._viewer, _Viewer(target, sock)
            self._viewer = viewer
        if previous is not None:
            previous.release()
        return viewer

    def _drop(self, viewer: _Viewer) -> None:
        with self._lock:
            if self._viewer is viewer:
                self._viewer = None
        viewer.release()

    def _watch_player(self, viewer: _Viewer) -> None:
        """Notice a player that left while the camera sends nothing: the copy loop only learns it when it writes.

        A player sends nothing after its request, so its socket turning readable means it closed (or broke the
        protocol); either way the viewer is let go, which also ends a read waiting on the upstream.
        """
        try:
            while not viewer.released:
                try:
                    readable, _, _ = select.select([viewer.sock], [], [], WATCH_SECONDS)
                except (OSError, ValueError):
                    break
                if readable:
                    break
        finally:
            if not viewer.released:
                self._drop(viewer)
            with self._lock:
                self._watchers.discard(threading.current_thread())

    def _open(self, viewer: _Viewer) -> Upstream | str:
        """Open ``viewer``'s upstream, or return the code of why it could not be opened."""
        if not self._admitted(viewer.target):
            return "not_admitted"
        try:
            upstream = self._opener(viewer.target)
        except (HttpError, TransportError) as exc:
            return _code(exc)
        except Exception:
            return "relay_error"
        if not (upstream.content_type or "").lower().startswith(MJPEG):
            upstream.close()
            return "not_mjpeg"
        with self._lock:
            current = self._viewer is viewer and not viewer.released
            if current:
                viewer.upstream = upstream
        if not current:
            upstream.close()
            return ""
        return upstream

    def _serve(self, handler: _Handler) -> None:
        target = self._resolve(handler.headers.get("Host"), handler.path, handler.server.server_port)
        if target is None:
            handler.refuse(404)
            return
        viewer = self._claim(target, handler.connection)
        if viewer is None:
            handler.refuse(404)
            return
        try:
            self._relay(handler, viewer)
        finally:
            self._drop(viewer)
            self._emit("ended", target)

    def _relay(self, handler: _Handler, viewer: _Viewer) -> None:
        opened = self._open(viewer)
        if isinstance(opened, str):
            if opened:
                self._emit("unavailable", viewer.target, opened)
            handler.refuse(502)
            return
        upstream: Upstream = opened
        content_type = upstream.content_type
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        watcher = threading.Thread(target=self._watch_player, args=(viewer,), daemon=True)
        with self._lock:
            self._watchers.add(watcher)
        watcher.start()
        pictured = False  # a picture reached this viewer at all
        while True:
            framed, tail = False, b""  # a picture came through this upstream
            while True:
                try:
                    chunk = upstream.read(CHUNK)
                except (HttpError, TransportError, OSError) as exc:
                    # A stalled or broken stream is a failure, not the end of a lease: no renewal.
                    if not viewer.released:
                        self._emit("unavailable", viewer.target, _code(exc))
                    return
                if not chunk:
                    break
                if not framed:
                    framed = _JPEG_START in tail + chunk
                    tail = chunk[-1:]
                    if framed and not pictured:
                        pictured = True
                        self._emit("frame", viewer.target)
                try:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                except OSError:
                    return  # the player left
            if viewer.released:
                return
            if not framed:
                if not pictured:
                    self._emit("unavailable", viewer.target, "no_picture")
                return
            # The server ended a stream that was working: its lease. Ask again; the server checks the key again.
            reopened = self._open(viewer)
            if isinstance(reopened, str):
                if reopened:
                    self._emit("unavailable", viewer.target, reopened)
                return
            if reopened.content_type != content_type:
                self._emit("unavailable", viewer.target, "not_mjpeg")
                return
            upstream = reopened


def _code(exc: BaseException) -> str:
    if isinstance(exc, HttpError):
        return exc.code or f"http_{exc.status}"
    if isinstance(exc, TransportError):
        return exc.kind
    return "connect"


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    timeout = REQUEST_TIMEOUT
    server_version = "OrcaBamDudeRelay"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        self.server.relay._serve(self)

    def _not_allowed(self) -> None:
        self.refuse(405)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_allowed  # noqa: N815

    def refuse(self, status: int) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass

    def log_message(self, format, *args) -> None:  # noqa: A002 - the stdlib's signature
        pass  # the request line carries the secret


class _Server(http.server.HTTPServer):
    allow_reuse_address = False  # SO_REUSEADDR on Windows would let another process take the port

    def __init__(self, relay: CameraRelay):
        self.relay = relay
        super().__init__((HOST, 0), _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks the address up by name; a loopback address needs no lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address) -> None:
        def serve() -> None:
            try:
                self.finish_request(request, client_address)
            except Exception:
                pass
            finally:
                self.shutdown_request(request)

        if not self.relay._accept(serve):
            self.shutdown_request(request)

    def handle_error(self, request, client_address) -> None:
        pass
