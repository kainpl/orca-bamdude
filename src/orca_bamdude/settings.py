"""The settings a user keeps: the stored base, the preset override on top of it, and what the tab may see.

Host spec r2.3.2 §4.4 ⟨R23-A1⟩ and §8.1.

- **Who owns it.** The connection belongs to the printer-agent capability. Its base lives in OrcaSlicer's plugin
  config file, and an override lives in a printer preset. The tab has no config of its own: it edits the agent's base.
- **What an override may carry.** Only the server address (with base path) and the CA. The API key and proxy headers
  come from base alone, because preset files are exported and synced. An override that names another field has
  that field ignored and reported.
- **A broken value is a problem, never a guess.** A stored field that does not validate is dropped and named in
  ``problems``. A broken override refuses the connection rather than falling back to base: the user asked for that
  preset's server, so another one is never silently used instead.
- **Nothing secret leaves this module through a view, an error or a target.**
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .config import (
    ConfigError,
    Connection,
    canonical_base_url,
    validate_api_key,
    validate_ca_pem,
    validate_extra_headers,
)

BASE_FIELDS = ("server_url", "api_key", "ca_pem", "headers", "export_dir")
OVERRIDE_FIELDS = ("server_url", "ca_pem")
_EDIT_ACTIONS = ("keep", "replace", "clear")


@dataclass(frozen=True)
class Settings:
    """The stored base, validated field by field."""

    server_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    ca_pem: str | None = field(default=None, repr=False)
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    export_dir: str | None = None
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Effective:
    """What the plugin connects with now, and where it came from."""

    source: str  # "base" or "preset"
    preset_name: str | None
    server_url: str | None
    ca_pem: str | None = field(repr=False)
    connection: Connection | None = field(repr=False)
    missing: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def _validate_export_dir(raw) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if not os.path.isabs(text):
        raise ConfigError("export_dir_relative")
    return os.path.normpath(text)


def _headers_from(raw) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list) or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in raw):
        raise ConfigError("header_shape")
    return validate_extra_headers(raw)


_PARSERS = {
    "server_url": lambda raw: canonical_base_url(raw) if str(raw or "").strip() else None,
    "api_key": lambda raw: validate_api_key(raw) if str(raw or "").strip() else None,
    "ca_pem": validate_ca_pem,
    "headers": lambda raw: _headers_from(raw) if raw else (),
    "export_dir": _validate_export_dir,
}


def parse_base(raw) -> Settings:
    """The stored base as found. Each field validates on its own; a broken one is dropped and named."""
    if not isinstance(raw, dict):
        return Settings(problems=("base:not_an_object",))
    values: dict = {}
    problems: list[str] = []
    for name in BASE_FIELDS:
        if name not in raw:
            continue
        try:
            values[name] = _PARSERS[name](raw[name])
        except ConfigError as exc:
            problems.append(f"{name}:{exc.code}")
    return Settings(**values, problems=tuple(problems))


def effective(base: Settings, override, preset_name: str | None = None) -> Effective:
    """Base with the allowed override fields on top (§4.4 ⟨R23-A1⟩).

    ``override`` is what ``get_config()`` returns for a preset that has one, ``None`` otherwise. An override without
    ``ca_pem`` keeps base's CA; ``ca_pem`` present and empty means the system's trust for that address.
    """
    if override is None:
        return _resolve("base", None, base.server_url, base.ca_pem, base, (), ())
    if not isinstance(override, dict):
        return Effective("preset", preset_name, None, None, None, problems=("override:not_an_object",))
    ignored = tuple(sorted(name for name in override if name not in OVERRIDE_FIELDS))
    server_url, ca_pem = base.server_url, base.ca_pem
    problems: list[str] = []
    for name in OVERRIDE_FIELDS:
        if name not in override:
            continue
        try:
            value = _PARSERS[name](override[name])
        except ConfigError as exc:
            problems.append(f"override:{name}:{exc.code}")
            continue
        if name == "server_url":
            server_url = value if value is not None else base.server_url
        else:
            ca_pem = value
    if problems:
        return Effective("preset", preset_name, None, None, None, ignored=ignored, problems=tuple(problems))
    return _resolve("preset", preset_name, server_url, ca_pem, base, ignored, ())


def _resolve(source, preset_name, server_url, ca_pem, base: Settings, ignored, problems) -> Effective:
    missing = tuple(name for name, value in (("server_url", server_url), ("api_key", base.api_key)) if not value)
    connection = None
    if not missing:
        connection = Connection(base_url=server_url, api_key=base.api_key, ca_pem=ca_pem, extra_headers=base.headers)
    return Effective(source, preset_name, server_url, ca_pem, connection, missing, ignored, problems)


def masked_view(base: Settings, current: Effective) -> dict:
    """What the tab is told: values that are not secret, and only whether a secret is set."""
    under_override = current.source == "preset"
    return {
        "server_url": current.server_url if under_override else base.server_url,
        "base_server_url": base.server_url,
        "api_key_set": bool(base.api_key),
        "ca_pem": current.ca_pem if under_override else base.ca_pem,
        "headers": [{"name": name, "value_set": bool(value)} for name, value in base.headers],
        "export_dir": base.export_dir,
        "source": current.source,
        "preset_name": current.preset_name,
        "read_only": list(OVERRIDE_FIELDS) if under_override else [],
        "ignored": [name for name in current.ignored if name in BASE_FIELDS],
        "missing": list(current.missing),
        "problems": list(base.problems) + list(current.problems),
    }


def apply_edit(stored: dict, edit: dict) -> dict:
    """The new stored base after an edit from the tab. Refused edits raise ``ConfigError`` naming no value.

    ``api_key`` is ``{"action": "keep" | "replace" | "clear", "value"?}``; ``headers`` is a list of
    ``{"name", "action"?: "keep", "value"?}`` -- ``keep`` reuses the stored value of that name, a header not listed is
    dropped. Other fields are plain values. A field not named is kept as stored.
    """
    if not isinstance(edit, dict) or any(name not in BASE_FIELDS for name in edit):
        raise ConfigError("edit_field")
    result = {name: stored[name] for name in BASE_FIELDS if isinstance(stored, dict) and name in stored}
    for name, value in edit.items():
        if name == "api_key":
            _edit_key(result, value)
        elif name == "headers":
            result["headers"] = _edit_headers(stored, value)
        elif name == "server_url":
            text = str(value or "").strip()
            result["server_url"] = canonical_base_url(text) if text else ""
        elif name == "ca_pem":
            result["ca_pem"] = validate_ca_pem(value) or ""
        else:
            result["export_dir"] = _validate_export_dir(value) or ""
    return result


def _edit_key(result: dict, value) -> None:
    action = value.get("action") if isinstance(value, dict) else None
    if action not in _EDIT_ACTIONS:
        raise ConfigError("edit_action")
    if action == "replace":
        result["api_key"] = validate_api_key(value.get("value"))
    elif action == "clear":
        result.pop("api_key", None)


def _edit_headers(stored, value) -> list[list[str]]:
    if not isinstance(value, list):
        raise ConfigError("header_shape")
    known = dict(parse_base(stored).headers) if isinstance(stored, dict) else {}
    pairs = []
    for item in value:
        if not isinstance(item, dict) or "name" not in item:
            raise ConfigError("header_shape")
        name = str(item["name"]).strip()
        if item.get("action", "replace") == "keep":
            if name not in known:
                raise ConfigError("header_keep_unknown")
            pairs.append((name, known[name]))
        else:
            pairs.append((name, str(item.get("value", ""))))
    return [list(pair) for pair in validate_extra_headers(pairs)]


def origin(url: str | None) -> str | None:
    """``scheme://host`` as OrcaSlicer stores a ``network=`` grant (port, path and query dropped)."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return f"{parts.scheme.lower()}://{f'[{host}]' if ':' in host else host.lower()}"


def dashboard_url(base_url: str | None) -> str | None:
    """The one URL "Open BamDude" may open (§8.5): exactly this string is declared as the process target."""
    return f"{base_url}/" if base_url else None


def slot_targets(base: Settings, current: Effective) -> dict[str, str]:
    """The permission target each user setting implies now, typed as ``<bucket>:<target>`` and free of secrets.

    A slot is one setting whose value can change: the base address, the effective address, the dashboard and the
    export folder. ``permits.observe`` compares slot by slot, so A -> B -> A counts as a change only when B was
    seen (§4.1 ⟨R23.1⟩). These are the targets an unfinished load may leave held (⟨R23-B2⟩); the plugin's fixed
    targets (threads, the temp model tree, the relay) are not among them.
    """
    slots = {}
    base_origin = origin(base.server_url)
    if base_origin:
        slots["base_network"] = f"network:{base_origin}"
    effective_origin = origin(current.server_url)
    if effective_origin:
        slots["effective_network"] = f"network:{effective_origin}"
    dashboard = dashboard_url(current.server_url)
    if dashboard:
        slots["dashboard"] = f"process:{dashboard}"
    if base.export_dir:
        slots["export_dir"] = f"fs_read:{base.export_dir}"
    return slots


def user_targets(base: Settings, current: Effective) -> set[str]:
    """Every target the user's settings imply now (see ``slot_targets``)."""
    return set(slot_targets(base, current).values())
