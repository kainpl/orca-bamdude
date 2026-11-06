"""The plugin's tests import its source tree directly; nothing here needs OrcaSlicer or the BamDude backend."""

import ipaddress
import socket
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fakes import Server  # noqa: E402 - after the source tree is on the path

from orca_bamdude.config import Connection  # noqa: E402
from orca_bamdude.transport import Transport  # noqa: E402

LOOPBACK_NAMES = {"localhost"}
_real_getaddrinfo = socket.getaddrinfo


def _loopback(host) -> bool:
    if host is None:
        return True  # a listening socket's wildcard
    name = host.decode() if isinstance(host, bytes) else str(host)
    if name.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _loopback(host):
        raise socket.gaierror(socket.EAI_NONAME, "tests never leave 127.0.0.1")
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    """No test reaches past loopback, not even a DNS query: the plugin's threads read a configured server as soon as
    OrcaSlicer asks, and a test's ``farm.example`` must fail here instead of leaving the machine."""
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


@pytest.fixture
def serve():
    """Start a scripted server; returns it and a transport to it under the base path ``/base``."""
    servers = []

    def start(routes, **conn):
        server = Server(routes)
        servers.append(server)
        connection = Connection.build(server.url + "/base", "bd_test_key", **conn)
        return server, Transport(connection, timeout=5)

    yield start
    for server in servers:
        server.close()
