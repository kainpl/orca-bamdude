"""The suite's own guard: no test resolves a name past loopback (conftest ``loopback_only``)."""

import http.client
import socket

import pytest


@pytest.mark.parametrize("name", ["farm.example", "192.0.2.10", "lan.example", b"farm.example"])
def test_a_name_past_loopback_is_refused(name):
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(name, 443)


@pytest.mark.parametrize("name", ["127.0.0.1", "127.0.0.5", "localhost", "::1"])
def test_loopback_resolves(name):
    assert socket.getaddrinfo(name, 80)


def test_an_http_request_to_a_farm_address_fails_before_any_packet():
    conn = http.client.HTTPSConnection("farm.example", 443, timeout=1)
    with pytest.raises(OSError):
        conn.request("GET", "/")
