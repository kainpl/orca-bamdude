"""A scripted local HTTP server standing in for BamDude: tests see exactly what the plugin sent."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Server:
    """Answers every request with ``routes(method, path) -> (status, headers, payload)``.

    ``payload`` is bytes (sent with its length) or an iterable of byte chunks (a stream: each chunk flushed as it is
    produced, the connection closed at the end).
    """

    def __init__(self, routes):
        self.routes = routes
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.seen.append(
                    {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
                )
                status, headers, payload = outer.routes(self.command, self.path)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if isinstance(payload, bytes | bytearray):
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.end_headers()
                for chunk in payload:
                    self.wfile.write(chunk)
                    self.wfile.flush()

            do_GET = do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.handle_error = lambda request, address: None  # a client that hung up on purpose
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def json_answer(status, payload, **headers):
    return status, {"Content-Type": "application/json", **headers}, json.dumps(payload).encode()
