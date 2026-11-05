"""Regenerate the TLS fixtures under ``tests/data/tls`` (test-only keys for a server on 127.0.0.1).

Not run by CI and not needed by the plugin: the files are committed, and this script only says how they were made.
It needs the ``cryptography`` package, which the plugin itself never imports.

- ``ca.pem`` -- the test CA the connection trusts (its text is what a user pastes as ``ca_pem``);
- ``server.pem`` / ``server-key.pem`` -- a server certificate for ``localhost`` only, signed by that CA, so the same
  server reached as ``127.0.0.1`` is a name mismatch;
- ``foreign-ca.pem`` -- a CA that signed nothing here.

Valid from 2026 to 2126, so the tests do not expire with the year.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

OUT = Path(__file__).resolve().parents[1] / "tests" / "data" / "tls"
NOT_BEFORE = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
NOT_AFTER = datetime.datetime(2126, 1, 1, tzinfo=datetime.UTC)


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _ca(common: str):
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common))
        .issuer_name(_name(common))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOT_BEFORE)
        .not_valid_after(NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _server(ca_key, ca_cert):
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOT_BEFORE)
        .not_valid_after(NOT_AFTER)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = _ca("OrcaBamDude test CA")
    _, foreign_cert = _ca("OrcaBamDude foreign test CA")
    server_key, server_cert = _server(ca_key, ca_cert)
    (OUT / "ca.pem").write_bytes(_pem(ca_cert))
    (OUT / "foreign-ca.pem").write_bytes(_pem(foreign_cert))
    (OUT / "server.pem").write_bytes(_pem(server_cert))
    (OUT / "server-key.pem").write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    print(f"written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
