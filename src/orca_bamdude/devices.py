"""Which OrcaSlicer device is which BamDude printer (host spec r2.3.2 §5.1).

A device id names a printer *of one binding*: the canonical server address with its base path, the server's
``connector_server_id`` and ``storage_epoch``, and the key's ``actor_id`` -- the four facts that say which server,
which state of it and whose view. Any of them changing (another address, a restore, another key, a preset override
with another address) makes every device new; an old id is then simply not in the map, and a late host callback for
it finds nothing to act on. The CA and the proxy headers are no part of it: the same target behind another
certificate is the same device.

A byte-for-byte copy of a server that replaced it at the same address without a restore cannot be told apart; the
supported way to move a server is backup/restore, which rotates the epoch.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .api import Capabilities
from .config import canonical_base_url

PREFIX = "bamdude-"


@dataclass(frozen=True)
class Binding:
    address: str
    server_id: str
    epoch: str
    actor_id: str

    @classmethod
    def of(cls, base_url: str, capabilities: Capabilities) -> Binding:
        return cls(
            canonical_base_url(base_url),
            capabilities.server_id,
            capabilities.storage_epoch,
            capabilities.actor_id,
        )


def dev_id(binding: Binding, printer_identity: str) -> str:
    """``bamdude-`` + the first 32 hex of SHA-256 over the compact JSON array of the binding and the printer."""
    encoded = json.dumps(
        [binding.address, binding.server_id, binding.epoch, binding.actor_id, printer_identity],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return PREFIX + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Device:
    dev_id: str
    printer_id: int
    identity: str
    name: str
    model_code: str | None


@dataclass(frozen=True)
class DeviceMap:
    """The printers one binding sees, in the server's order. Only a printer with a known model is announced to
    OrcaSlicer, and only an announced one can be looked up: Orca was never shown the others."""

    binding: Binding
    devices: tuple[Device, ...]

    def announced(self) -> list[Device]:
        return [d for d in self.devices if d.model_code]

    def unannounced(self) -> list[tuple[Device, str]]:
        return [(d, "model_unknown") for d in self.devices if not d.model_code]

    def get(self, device_id: str) -> Device | None:
        return next((d for d in self.announced() if d.dev_id == device_id), None)

    def by_identity(self, identity: str) -> Device | None:
        return next((d for d in self.devices if d.identity == identity), None)


def _device(binding: Binding, entry) -> Device | None:
    if not isinstance(entry, dict):
        return None
    pid, identity, name, model = (entry.get(k) for k in ("id", "connector_identity", "name", "model_code"))
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(identity, str) or not identity:
        return None
    if name is not None and not isinstance(name, str):
        return None
    if model is not None and not isinstance(model, str):
        return None
    return Device(dev_id(binding, identity), pid, identity, name or f"#{pid}", model or None)


def device_map(binding: Binding, printers) -> DeviceMap:
    """The map of a printers list as the server sent it. An entry that is not well formed is left out, and so is
    every printer whose identity another one repeats -- neither could be told apart."""
    found = [d for d in (_device(binding, entry) for entry in printers or []) if d is not None]
    counts: dict[str, int] = {}
    for device in found:
        counts[device.identity] = counts.get(device.identity, 0) + 1
    return DeviceMap(binding, tuple(d for d in found if counts[d.identity] == 1))


__all__ = ["Binding", "Device", "DeviceMap", "dev_id", "device_map"]
