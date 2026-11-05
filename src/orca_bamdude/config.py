"""The connection a user configures: one BamDude server, one API key, an optional CA and proxy headers (spec r2 §12).

Validated once, here, and frozen. Nothing in this module talks to the network.

- The server URL is ``http`` or ``https`` only, with no user info, query or fragment; its path is kept as the
  server's base path. Plain HTTP is allowed as an explicit choice (a LAN or VPN), never substituted for HTTPS.
- Proxy headers (a Cloudflare Access pair, for example) have valid names and values without CR/LF, are bounded in
  number and size, and may never override what the connector itself sends or what a proxy reserves.
- A private CA is PEM text the user pastes (host spec r2.2 §12), never a path: OrcaSlicer's audit refuses path
  components such as ``cert`` or ``certs``, and text needs no file at all. Only CERTIFICATE blocks, bounded, and
  loaded by ``ssl`` before it is accepted; no text means the system's trust.
"""

from __future__ import annotations

import re
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

MAX_EXTRA_HEADERS = 8
MAX_HEADER_VALUE = 1024
MAX_CA_PEM = 64 * 1024
_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END ([A-Z0-9 ]+)-----", re.S)
_PEM_MARKER = re.compile(r"-----(BEGIN|END) [A-Z0-9 ]+-----")
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
# What the connector sends itself, what HTTP owns, and what a fronting proxy writes on its own.
RESERVED_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "x-api-key",
        "x-bamdude-printer-identity",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "cookie",
        "cf-connecting-ip",
        "cf-ipcountry",
        "cf-ray",
        "cf-visitor",
        "cf-worker",
        "cdn-loop",
        "true-client-ip",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    }
)


class ConfigError(ValueError):
    """A configuration value is refused; ``code`` is stable, the text never contains the value itself."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical_base_url(raw: str) -> str:
    """The server's base URL, canonical: lower-case scheme and host, no trailing slash, nothing after the path."""
    text = (raw or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ConfigError("url_invalid") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigError("url_scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ConfigError("url_userinfo")
    if parts.query or parts.fragment or text.endswith("?") or text.endswith("#"):
        raise ConfigError("url_query")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ConfigError("url_invalid") from exc
    if not host:
        raise ConfigError("url_host")
    if any(ch.isspace() for ch in text):
        raise ConfigError("url_invalid")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def validate_extra_headers(pairs) -> tuple[tuple[str, str], ...]:
    """Proxy headers as ``(name, value)`` pairs, each one checked; names are unique regardless of case."""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in pairs or ():
        name = str(name).strip()
        value = str(value)
        if not _TOKEN.match(name):
            raise ConfigError("header_name")
        lowered = name.lower()
        if lowered in RESERVED_HEADERS or lowered in seen:
            raise ConfigError("header_reserved")
        if any(ch in value for ch in "\r\n\0") or len(value) > MAX_HEADER_VALUE:
            raise ConfigError("header_value")
        seen.add(lowered)
        result.append((name, value))
    if len(result) > MAX_EXTRA_HEADERS:
        raise ConfigError("header_count")
    return tuple(result)


def validate_api_key(raw: str) -> str:
    key = (raw or "").strip()
    if not key or any(ch.isspace() or ord(ch) < 0x21 or ord(ch) > 0x7E for ch in key) or len(key) > 256:
        raise ConfigError("api_key")
    return key


def validate_ca_pem(raw: str | None) -> str | None:
    """The CA as PEM text, stripped -- or ``None`` (the system's trust) when there is none.

    Refused: more than ``MAX_CA_PEM`` bytes (``ca_pem_size``); a block that is not a CERTIFICATE, such as a private
    key pasted by mistake (``ca_pem_block``); no complete block, a marker outside one, or text ``ssl`` cannot load
    as certificates (``ca_pem_invalid``). The error never carries the text.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if len(text.encode("utf-8")) > MAX_CA_PEM:
        raise ConfigError("ca_pem_size")
    blocks = _PEM_BLOCK.findall(text)
    if any(begin != "CERTIFICATE" or end != "CERTIFICATE" for begin, _body, end in blocks):
        raise ConfigError("ca_pem_block")
    if not blocks or len(_PEM_MARKER.findall(text)) != 2 * len(blocks):
        raise ConfigError("ca_pem_invalid")
    try:
        ssl.create_default_context(cadata=text)
    except (ssl.SSLError, ValueError) as exc:
        raise ConfigError("ca_pem_invalid") from exc
    return text


@dataclass(frozen=True)
class Connection:
    base_url: str
    api_key: str = field(repr=False)
    ca_pem: str | None = field(default=None, repr=False)
    extra_headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def uses_tls(self) -> bool:
        return self.base_url.startswith("https://")

    @classmethod
    def build(cls, base_url: str, api_key: str, ca_pem: str | None = None, extra_headers=None) -> Connection:
        return cls(
            base_url=canonical_base_url(base_url),
            api_key=validate_api_key(api_key),
            ca_pem=validate_ca_pem(ca_pem),
            extra_headers=validate_extra_headers(extra_headers),
        )
