"""The plugin's tests import its source tree directly; nothing here needs OrcaSlicer or the BamDude backend."""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fakes import Server  # noqa: E402 - after the source tree is on the path

from orca_bamdude.config import Connection  # noqa: E402
from orca_bamdude.transport import Transport  # noqa: E402


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
