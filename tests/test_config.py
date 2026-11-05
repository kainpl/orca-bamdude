from pathlib import Path

import pytest

from orca_bamdude.config import (
    MAX_CA_PEM,
    ConfigError,
    Connection,
    canonical_base_url,
    validate_ca_pem,
    validate_extra_headers,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://farm.example", "https://farm.example"),
        ("HTTPS://Farm.Example/", "https://farm.example"),
        ("http://192.168.1.5:8000", "http://192.168.1.5:8000"),
        ("https://farm.example/bamdude/", "https://farm.example/bamdude"),
        ("  https://farm.example:8443/a/b  ", "https://farm.example:8443/a/b"),
        ("http://[::1]:8000", "http://[::1]:8000"),
    ],
)
def test_a_server_url_is_canonical(raw, expected):
    assert canonical_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw,code",
    [
        ("ftp://farm.example", "url_scheme"),
        ("farm.example", "url_scheme"),
        ("https://user:pass@farm.example", "url_userinfo"),
        ("https://farm.example/?a=1", "url_query"),
        ("https://farm.example/#x", "url_query"),
        ("https://farm.example?", "url_query"),
        ("https://", "url_host"),
        ("https://farm.example:notaport", "url_invalid"),
        ("https://farm example", "url_invalid"),
    ],
)
def test_a_server_url_outside_the_rules_is_refused(raw, code):
    with pytest.raises(ConfigError) as refused:
        canonical_base_url(raw)
    assert refused.value.code == code


def test_proxy_headers_are_checked_and_kept():
    pairs = validate_extra_headers([("CF-Access-Client-Id", "id"), ("CF-Access-Client-Secret", "secret")])
    assert pairs == (("CF-Access-Client-Id", "id"), ("CF-Access-Client-Secret", "secret"))


@pytest.mark.parametrize(
    "pairs,code",
    [
        ([("X-API-Key", "x")], "header_reserved"),
        ([("Authorization", "x")], "header_reserved"),
        ([("host", "x")], "header_reserved"),
        ([("CF-Connecting-IP", "x")], "header_reserved"),
        ([("A", "1"), ("a", "2")], "header_reserved"),
        ([("Bad Name", "x")], "header_name"),
        ([("X-Ok", "line\r\nInjected: 1")], "header_value"),
        ([("X-Ok", "x" * 2000)], "header_value"),
        ([(f"X-{n}", "v") for n in range(9)], "header_count"),
    ],
)
def test_proxy_headers_outside_the_rules_are_refused(pairs, code):
    with pytest.raises(ConfigError) as refused:
        validate_extra_headers(pairs)
    assert refused.value.code == code


def test_a_connection_never_shows_its_key():
    connection = Connection.build("https://farm.example", "bd_secret_value")
    assert "bd_secret_value" not in repr(connection)
    with pytest.raises(ConfigError):
        Connection.build("https://farm.example", "has space")


TLS = Path(__file__).parent / "data" / "tls"
CA = (TLS / "ca.pem").read_text(encoding="ascii")
FOREIGN_CA = (TLS / "foreign-ca.pem").read_text(encoding="ascii")
KEY = (TLS / "server-key.pem").read_text(encoding="ascii")


def test_a_ca_is_pem_text_kept_as_given():
    connection = Connection.build("https://farm.example", "bd_x", ca_pem="\n  " + CA + "\n")

    assert connection.ca_pem == CA.strip()
    assert validate_ca_pem(CA + FOREIGN_CA) == (CA + FOREIGN_CA).strip()


@pytest.mark.parametrize("empty", [None, "", "   \n\t"], ids=["none", "empty", "blank"])
def test_no_ca_means_the_systems_trust(empty):
    assert validate_ca_pem(empty) is None
    assert Connection.build("https://farm.example", "bd_x", ca_pem=empty).ca_pem is None


@pytest.mark.parametrize(
    "text,code",
    [
        (KEY, "ca_pem_block"),
        (CA + KEY, "ca_pem_block"),
        ("-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n", "ca_pem_block"),
        ("not a certificate at all", "ca_pem_invalid"),
        ("-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n-----END CERTIFICATE-----\n", "ca_pem_invalid"),
        ("-----BEGIN CERTIFICATE-----\n" + CA.splitlines()[1] + "\n", "ca_pem_invalid"),
        (CA + "\n" + "x" * MAX_CA_PEM, "ca_pem_size"),
    ],
    ids=["private-key", "ca-and-key", "public-key", "no-block", "not-a-certificate", "unterminated", "too-large"],
)
def test_a_ca_outside_the_rules_is_refused_without_echoing_it(text, code):
    with pytest.raises(ConfigError) as refused:
        Connection.build("https://farm.example", "bd_x", ca_pem=text)

    assert refused.value.code == code
    assert str(refused.value) == code
