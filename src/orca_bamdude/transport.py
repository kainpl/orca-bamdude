"""HTTP to one configured server, and nothing else (spec r2 §12).

- TLS is verified -- against the pasted CA (``cadata``) for a self-signed LAN server, else the system's trust --
  always with the host name checked; there is no switch to turn it off and no temporary file.
- No redirect is followed, to any origin: a 3xx is a refusal the user fixes by entering the final URL.
- Credentials go only to the configured authority. Responses are bounded; an HTML or other non-JSON body is a
  protocol error, never shown. An error's text is the server's ``detail.message`` at most -- never a raw body,
  a header, the key or a stack.
- Every call has a timeout. An upload streams a file in chunks and can be cancelled between chunks.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
from collections.abc import Callable, Iterable
from urllib.parse import urlencode, urlsplit

from orca_bamdude.config import Connection

USER_AGENT = "OrcaBamDude"
MAX_JSON_RESPONSE = 4 * 1024 * 1024
MAX_MESSAGE = 500
UPLOAD_CHUNK = 256 * 1024


class TransportError(Exception):
    """The request did not get a usable answer. ``kind`` is stable: timeout, tls, connect, redirect, protocol,
    too_large, cancelled."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class HttpError(Exception):
    """The server answered with a refusal: its status, its stable code and its (already translated) sentence."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(f"{status} {code}")
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _ssl_context(ca_pem: str | None) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _retry_after(response: http.client.HTTPResponse) -> float | None:
    value = response.getheader("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def _read_bounded(response: http.client.HTTPResponse, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise TransportError("too_large")
    return data


def _refusal(status: int, payload) -> HttpError:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        code = str(detail.get("error") or "http_error")[:64]
        message = str(detail.get("message") or "")[:MAX_MESSAGE]
    else:
        code, message = "http_error", (detail[:MAX_MESSAGE] if isinstance(detail, str) else "")
    return HttpError(status, code, message)


class Transport:
    def __init__(self, connection: Connection, *, timeout: float = 10.0):
        self.connection = connection
        self.timeout = timeout
        parts = urlsplit(connection.base_url)
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port
        self._base_path = parts.path
        self._context = _ssl_context(connection.ca_pem) if parts.scheme == "https" else None
        self._lock = threading.Lock()
        self._live: set[http.client.HTTPConnection] = set()
        self._closed = False

    # ------------------------------------------------------------------ the owner's cancel (H2 review R3)

    def close(self) -> None:
        """End every request in flight and refuse every new one with ``cancelled``: the owner's unload or a new
        connection generation. A stream already handed to its caller is the caller's to close."""
        with self._lock:
            self._closed = True
            live = list(self._live)
        for conn in live:
            sock = conn.sock
            if sock is not None:
                _abort(sock)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _failure(self, kind: str) -> TransportError:
        """What a failure means: once the owner closed the transport, every failure is its cancel."""
        return TransportError("cancelled" if self.closed else kind)

    def _connect(self, conn: http.client.HTTPConnection) -> None:
        """Connect before anything is sent, then look again: a transport closed meanwhile sends nothing."""
        try:
            conn.connect()
        except ssl.SSLError as exc:
            raise self._failure("tls") from exc
        except TimeoutError as exc:
            raise self._failure("timeout") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise self._failure("connect") from exc
        if self.closed:
            raise TransportError("cancelled")

    def _forget(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._live.discard(conn)
        conn.close()

    # ------------------------------------------------------------------ plumbing

    def _open(self, timeout: float | None = None) -> http.client.HTTPConnection:
        if self._scheme == "https":
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=timeout or self.timeout, context=self._context
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=timeout or self.timeout)
        with self._lock:
            if self._closed:
                raise TransportError("cancelled")
            self._live.add(conn)
        return conn

    def _target(self, path: str, query: dict | Iterable | None) -> str:
        if not path.startswith("/api/v1/"):
            raise ValueError("only API paths are requested")
        target = self._base_path + path
        if query:
            target += "?" + urlencode(query, doseq=True)
        return target

    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(self.connection.extra_headers)
        headers.update({"User-Agent": USER_AGENT, "X-API-Key": self.connection.api_key, "Accept": "application/json"})
        headers.update(extra or {})
        return headers

    def _send(self, conn: http.client.HTTPConnection, method: str, target: str, body, headers: dict):
        self._connect(conn)
        try:
            conn.request(method, target, body=body, headers=headers)
            return conn.getresponse()
        except ssl.SSLError as exc:
            raise self._failure("tls") from exc
        except TimeoutError as exc:
            raise self._failure("timeout") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise self._failure("connect") from exc

    def _json(self, response: http.client.HTTPResponse) -> tuple[int, object]:
        status = response.status
        if 300 <= status < 400:
            raise TransportError("redirect")
        try:
            raw = _read_bounded(response, MAX_JSON_RESPONSE)
        except TimeoutError as exc:
            raise self._failure("timeout") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise self._failure("connect") from exc
        content_type = (response.getheader("Content-Type") or "").lower()
        payload = None
        if raw:
            if "json" not in content_type:
                if status >= 400:
                    raise HttpError(status, "http_error", "", _retry_after(response))
                raise TransportError("protocol")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise TransportError("protocol") from exc
        if status >= 400:
            refusal = _refusal(status, payload)
            refusal.retry_after = _retry_after(response)
            raise refusal
        return status, payload

    # ------------------------------------------------------------------ requests

    def request_json(self, method: str, path: str, *, query=None, body: dict | None = None) -> tuple[int, object]:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        headers = self._headers({"Content-Type": "application/json"} if encoded is not None else None)
        conn = self._open()
        try:
            response = self._send(conn, method, self._target(path, query), encoded, headers)
            return self._json(response)
        finally:
            self._forget(conn)

    def upload_file(
        self,
        path: str,
        file_path: str,
        size: int,
        *,
        query=None,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        timeout: float = 300.0,
    ) -> tuple[int, object]:
        """Stream a file as the raw body. Cancelling between chunks closes the connection; the server discards it."""
        headers = self._headers({"Content-Type": "application/octet-stream", "Content-Length": str(size)})
        conn = self._open(timeout)
        try:
            self._connect(conn)
            try:
                conn.putrequest("POST", self._target(path, query), skip_accept_encoding=True)
                for name, value in headers.items():
                    conn.putheader(name, value)
                conn.endheaders()
                sent = 0
                with open(file_path, "rb") as handle:
                    while True:
                        if cancelled is not None and cancelled():
                            raise TransportError("cancelled")
                        chunk = handle.read(UPLOAD_CHUNK)
                        if not chunk:
                            break
                        conn.send(chunk)
                        sent += len(chunk)
                        if progress is not None:
                            progress(sent, size)
                response = conn.getresponse()
            except TransportError:
                raise
            except ssl.SSLError as exc:
                raise self._failure("tls") from exc
            except TimeoutError as exc:
                raise self._failure("timeout") from exc
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                raise self._failure("connect") from exc
            return self._json(response)
        finally:
            self._forget(conn)

    def open_stream(self, path: str, *, headers: dict | None = None, query=None, timeout: float = 15.0) -> Stream:
        """An open streaming answer (the camera); the caller reads it and closes it. Never follows a redirect.

        ``timeout`` bounds each read, so a camera that stalls ends the stream instead of holding a thread.
        """
        conn = self._open(timeout)
        try:
            self._connect(conn)
            try:
                conn.request("GET", self._target(path, query), headers=self._headers(headers or {}))
                # Kept before the answer is read: http.client may hand the socket to the response and forget it,
                # and only the socket lets another thread end a read that is waiting for the next frame.
                sock = conn.sock
                response = conn.getresponse()
            except ssl.SSLError as exc:
                raise self._failure("tls") from exc
            except TimeoutError as exc:
                raise self._failure("timeout") from exc
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                raise self._failure("connect") from exc
            if 300 <= response.status < 400:
                raise TransportError("redirect")
            if response.status >= 400:
                self._json(response)  # raises the server's refusal
        except BaseException:
            self._forget(conn)
            raise
        with self._lock:
            self._live.discard(conn)  # the stream is its caller's now: closing the transport no longer touches it
            closed = self._closed
        stream = Stream(conn, sock, response)
        if closed:
            stream.close()
            raise TransportError("cancelled")
        return stream


class Stream:
    """One open streaming answer. ``read`` returns what has arrived (up to ``size``) and ``b""`` at the end or once
    closed; ``close`` may be called from another thread and ends a read that is waiting.

    ``http.client``'s response is not thread-safe: a read that reaches the end closes the response itself, and a
    second thread closing it at the same moment breaks it. So the response has one owner at a time. ``close`` from
    another thread only wakes a waiting read (``_abort``); the read, on its way out, releases the response. A
    ``close`` while nothing is reading releases it at once.
    """

    def __init__(self, conn: http.client.HTTPConnection, sock, response: http.client.HTTPResponse):
        self._conn = conn
        self._sock = sock
        self._response = response
        self._lock = threading.Lock()
        self._closed = False
        self._reading = False
        self._released = False
        self.content_type = response.getheader("Content-Type") or ""

    def read(self, size: int) -> bytes:
        with self._lock:
            if self._closed:
                return b""
            self._reading = True
        try:
            return self._response.read1(size)
        except Exception as exc:
            if self._closed:
                return b""  # woken by close(): whatever the response raised on the way out is not a failure
            if isinstance(exc, TimeoutError):
                raise TransportError("timeout") from exc
            if isinstance(exc, OSError | ValueError | http.client.HTTPException):
                raise TransportError("connect") from exc
            raise
        finally:
            with self._lock:
                self._reading = False
                release = self._closed
            if release:
                self._release()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            reading = self._reading
            sock, self._sock = self._sock, None
        if sock is not None:
            _abort(sock)
        if not reading:
            self._release()

    def _release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        try:
            self._response.close()
        except Exception:
            pass
        self._conn.close()


def _abort(sock) -> None:
    """End a socket another thread may be reading from.

    ``shutdown`` wakes a waiting read on Linux and macOS; the descriptor is then closed by ``response.close()``,
    after the reader has let go of it. It must not be closed here: the reader may already hold its number, the OS
    may give that number to the next socket anyone opens, and the reader would then wait on a stranger's socket
    until its timeout (seen on macOS in CI). Windows is the exception: ``shutdown`` does not wake a waiting
    ``recv`` there, only closing the handle does, and the response's file object keeps it open after ``close()``,
    so on Windows it is detached and closed here.
    """
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    if os.name != "nt":
        return
    try:
        fd = sock.detach()
    except (OSError, ValueError):
        return
    if fd is not None and fd >= 0:
        try:
            socket.socket(fileno=fd).close()
        except OSError:
            pass
